#!/usr/bin/env python3
"""
Chạy TRONG GitHub Actions (không chạy trên HF Space).

Nhiệm vụ: gọi baidupcs-py để resolve link share Baidu Pan (kèm passcode nếu
có) rồi transfer thẳng vào Cloud cá nhân, sau đó POST kết quả (thành công
kèm danh sách path đã lưu, hoặc lỗi kèm errno/errmsg debug) về webhook của
HF Space.

shared_paths() và transfer_shared_paths() PHẢI chạy trong CÙNG 1 process vì
kết quả của shared_paths() là các object Python (mang uk/shareid) không thể
serialize qua JSON để dùng lại ở process khác — đây là lý do toàn bộ bước
"transfer vào cloud" phải nằm gọn trong GitHub Actions, không tách sang HF
Space.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from baidupcs_py.baidupcs import BaiduPCSApi

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("resolve_baidu_share")


def _log_env_snapshot() -> None:
    """Log NGAY LẬP TỨC tình trạng có/không của toàn bộ biến môi trường quan
    trọng — chạy TRƯỚC mọi lệnh _env() bắt buộc, để nếu có lỗi "Thiếu biến
    môi trường bắt buộc: X" thì log vẫn cho thấy TOÀN CẢNH biến nào có/thiếu
    trong CÙNG 1 lần chạy, thay vì chỉ báo đúng 1 biến đầu tiên rồi dừng —
    giúp debug nhanh khi workflow.yml bị thiếu/sai tên key trong `env:`.
    Giá trị nhạy cảm (BDUSS/STOKEN/secret/token) chỉ log ĐỘ DÀI, không log
    giá trị thật."""
    sensitive = {"BAIDU_BDUSS", "BAIDU_STOKEN", "BAIDU_WEBHOOK_SECRET", "HF_TOKEN"}
    names = [
        "JOB_ID", "SHARE_URL", "PASSCODE", "DEST_DIR", "CALLBACK_URL",
        "BAIDU_BDUSS", "BAIDU_STOKEN", "BAIDU_WEBHOOK_SECRET", "HF_TOKEN",
    ]
    logger.debug("--- Env snapshot lúc khởi động script ---")
    for name in names:
        raw = os.getenv(name, "")
        if not raw:
            logger.debug("  %s: (RỖNG/KHÔNG CÓ)", name)
        elif name in sensitive:
            logger.debug("  %s: có giá trị (độ dài %d ký tự)", name, len(raw))
        else:
            logger.debug("  %s: %r", name, raw)
    logger.debug("------------------------------------------")


def _env(name: str, required: bool = True) -> str:
    val = os.getenv(name, "").strip()
    if required and not val:
        logger.error("Thiếu biến môi trường bắt buộc: %s", name)
        sys.exit(1)
    return val


def _extract_errno_errmsg(raw: Any) -> tuple[Any, Any]:
    """Bóc errno/errmsg từ RAW response hoặc exception — dùng để debug khi
    kết quả rỗng hoặc lib raise lỗi mập mờ."""
    errno = None
    errmsg = None
    if raw is None:
        return errno, errmsg

    if isinstance(raw, dict):
        errno = raw.get("errno")
        errmsg = raw.get("errmsg") or raw.get("err_msg") or raw.get("show_msg")
        if errno is not None or errmsg is not None:
            return errno, errmsg

    for attr in ("errno", "error_code", "code"):
        val = getattr(raw, attr, None)
        if val is not None:
            errno = val
            break
    for attr in ("errmsg", "err_msg", "message", "show_msg"):
        val = getattr(raw, attr, None)
        if val:
            errmsg = val
            break

    if isinstance(raw, BaseException) and errno is None and errmsg is None:
        msg = str(raw)
        m_errno = re.search(r"errno[\"']?\s*[:=]\s*(-?\d+)", msg, re.IGNORECASE)
        m_errmsg = re.search(r"err(?:_)?msg[\"']?\s*[:=]\s*[\"']?([^\"',}]+)", msg, re.IGNORECASE)
        if m_errno:
            errno = m_errno.group(1)
        if m_errmsg:
            errmsg = m_errmsg.group(1).strip()

    return errno, errmsg


def _entry_path(entry: Any) -> str | None:
    if isinstance(entry, dict):
        return entry.get("path") or entry.get("remotepath")
    return getattr(entry, "path", None) or getattr(entry, "remotepath", None)


def _build_full_share_url(share_url: str, passcode: str) -> str:
    """Ghép passcode vào URL dưới dạng query string `?pwd=...` — BẮT BUỘC
    vì shared_paths() của baidupcs-py CHỈ đọc pwd từ NGAY TRONG URL, không
    nhận tham số password/passcode riêng (đã xác nhận qua log lỗi thực tế
    ở các lượt trước). Nếu HF Space gửi `share_url` và `passcode` TÁCH RỜI
    nhau trong client_payload (đúng tình huống gây lỗi "danh sách rỗng" lần
    này), URL gốc sẽ KHÔNG có ?pwd=, khiến Baidu coi đây là truy cập không
    mật khẩu và trả về rỗng dù passcode có đúng đi nữa.

    Nếu URL đã có sẵn `?pwd=...` (khác rỗng) thì GIỮ NGUYÊN, không ghi đè —
    tôn trọng giá trị đã có trong URL gốc."""
    if not passcode:
        return share_url

    parsed = urlparse(share_url)
    query = parse_qs(parsed.query)
    if query.get("pwd") and query["pwd"][0]:
        return share_url

    query["pwd"] = [passcode]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _looks_like_captcha_or_cookie_issue(exc_or_text: Any) -> bool:
    """Nhận diện sơ bộ (dựa trên keyword) xem lỗi có khả năng do CAPTCHA/mã
    xác minh (vcode) hoặc Cookie (BDUSS/STOKEN) đã hết hạn hay không — giúp
    error_message trả về HF Space đủ cụ thể để không phải đoán mò."""
    text = str(exc_or_text).lower()
    keywords = (
        "vcode", "captcha", "verify", "验证码", "登录", "login",
        "unauthorized", "-6", "cookie", "bduss", "stoken",
    )
    return any(kw in text for kw in keywords)


def _send_callback(callback_url: str, webhook_secret: str, job_id: str, payload: dict) -> None:
    body = {"job_id": job_id, **payload}
    logger.info("Gửi callback về %s: %s", callback_url, json.dumps(body, ensure_ascii=False))

    headers = {"X-Webhook-Secret": webhook_secret}
    # HF Space Private yêu cầu xác thực bằng HF Token (Authorization: Bearer)
    # để request từ bên ngoài vượt qua được lớp gate riêng của HF Spaces —
    # thiếu header này thì request sẽ bị chặn TRƯỚC KHI chạm tới route
    # /baidu_webhook, không liên quan gì tới BAIDU_WEBHOOK_SECRET (2 lớp xác
    # thực độc lập: HF_TOKEN gác cửa HF Space, BAIDU_WEBHOOK_SECRET gác cửa
    # nội bộ route webhook).
    hf_token = os.getenv("HF_TOKEN", "").strip()
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    try:
        resp = requests.post(
            callback_url,
            json=body,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        logger.info("Callback gửi thành công (HTTP %s)", resp.status_code)
    except Exception as exc:  # noqa: BLE001
        # Log lỗi callback nhưng KHÔNG raise tiếp — nếu webhook fail thì HF
        # Space sẽ tự timeout sau X giây, log ở đây chỉ để debug qua GH
        # Actions log khi cần.
        logger.error("Gửi callback THẤT BẠI: %s", exc)


def main() -> None:
    _log_env_snapshot()

    job_id = _env("JOB_ID")
    share_url = _env("SHARE_URL")
    passcode = _env("PASSCODE", required=False)
    dest_dir = _env("DEST_DIR")
    callback_url = _env("CALLBACK_URL")
    webhook_secret = _env("BAIDU_WEBHOOK_SECRET")
    bduss = _env("BAIDU_BDUSS")
    stoken = _env("BAIDU_STOKEN", required=False)

    logger.info("Job %s — resolving share_url=%s -> dest_dir=%s", job_id, share_url, dest_dir)

    try:
        api = BaiduPCSApi(bduss=bduss, stoken=stoken or None)

        # ------------------------------------------------------------- #
        # ƯU TIÊN save_shared(url, remotedir, password=...) — hàm cấp cao
        # của baidupcs-py, tương ứng lệnh CLI chính thức:
        #   BaiduPCS-Py save <shared_url> <remotedir> --password xxxx
        # Hàm này tự lo TOÀN BỘ luồng verify passcode (gọi /share/verify,
        # lưu cookie BDCLND) NGẦM bên trong — không cần tự ghép ?pwd= vào
        # URL (cách đó đã xác nhận KHÔNG hoạt động ở lần thử trước, vì
        # shared_paths() cấp thấp không tự đọc pwd từ URL trong bản đang
        # cài). Đây CHÍNH XÁC là nhánh mà baidu_downloader.py bên HF Space
        # đã dùng ưu tiên từ đầu — resolve_baidu_share.py trước đó bỏ sót
        # nhánh này, đi thẳng vào shared_paths() nên luôn nhận link như
        # KHÔNG có passcode.
        # ------------------------------------------------------------- #
        save_fn = getattr(api, "save_shared", None)

        if callable(save_fn):
            logger.info(
                "Dùng save_shared(url, remotedir, password=...) — verify "
                "passcode ngầm bên trong hàm này.",
            )
            try:
                save_result = save_fn(share_url, dest_dir, password=passcode or None)
            except Exception as exc:  # noqa: BLE001
                errno, errmsg = _extract_errno_errmsg(exc)
                logger.error(
                    "save_shared(%s, %s) raise exception — errno=%s errmsg=%s raw=%r",
                    share_url, dest_dir, errno, errmsg, exc,
                )
                if _looks_like_captcha_or_cookie_issue(exc):
                    raise RuntimeError(
                        f"save_shared() lỗi nghi do sai passcode, CAPTCHA/mã "
                        f"xác minh, hoặc Cookie BDUSS/STOKEN đã hết hạn: {exc}"
                    ) from exc
                raise

            logger.debug("RAW save_shared(): %r", save_result)

            # save_shared() thường không trả về danh sách path chi tiết như
            # shared_paths() (tuỳ version) — cố bóc nếu có, còn không thì
            # coi hàm chạy xong KHÔNG raise là đã thành công.
            saved_paths = [
                p for p in (_entry_path(e) for e in (save_result or [])) if p
            ] if isinstance(save_result, (list, tuple)) else []

            logger.info(
                "Transfer (qua save_shared) thành công — %d path xác định "
                "được (có thể lib không trả chi tiết) -> %s",
                len(saved_paths), dest_dir,
            )

            _send_callback(
                callback_url, webhook_secret, job_id,
                {
                    "status": "success",
                    "dest_dir": dest_dir,
                    "saved_paths": saved_paths,
                },
            )
            return

        # ------------------------------------------------------------- #
        # FALLBACK — bản baidupcs-py đang cài không có save_shared(), quay
        # về cặp shared_paths()/transfer_shared_paths() cấp thấp. Ở đây
        # KHÔNG tự ghép ?pwd= vào URL nữa (đã xác nhận không tác dụng) —
        # nếu link có passcode mà không có save_shared() để verify hộ, báo
        # lỗi rõ ràng luôn thay vì âm thầm trả về danh sách rỗng khó hiểu.
        # ------------------------------------------------------------- #
        if passcode:
            raise RuntimeError(
                "Link có passcode nhưng bản baidupcs-py hiện tại KHÔNG có "
                "hàm save_shared() để verify passcode — cần "
                "`pip install -U baidupcs-py` lên bản có hỗ trợ save_shared "
                "(hoặc share/verify), vì shared_paths() cấp thấp không tự "
                "đọc pwd từ URL/tham số."
            )

        logger.info("Dùng shared_paths()/transfer_shared_paths() (fallback, link không passcode).")

        try:
            shared_paths = api.shared_paths(share_url)
        except Exception as exc:  # noqa: BLE001
            errno, errmsg = _extract_errno_errmsg(exc)
            logger.error(
                "shared_paths(%s) raise exception — errno=%s errmsg=%s raw=%r",
                share_url, errno, errmsg, exc,
            )
            if _looks_like_captcha_or_cookie_issue(exc):
                raise RuntimeError(
                    f"shared_paths() lỗi nghi do CAPTCHA/mã xác minh hoặc "
                    f"Cookie BDUSS/STOKEN đã hết hạn: {exc}"
                ) from exc
            raise

        logger.debug("RAW shared_paths(): %r", shared_paths)

        if not shared_paths:
            errno, errmsg = _extract_errno_errmsg(shared_paths)
            logger.error("shared_paths() trả về rỗng — errno=%s errmsg=%s", errno, errmsg)
            _send_callback(
                callback_url, webhook_secret, job_id,
                {
                    "status": "error",
                    "error_message": (
                        "Danh sách file rỗng, link không passcode — link có "
                        "thể đã die, bị gỡ, hoặc Cookie BAIDU_BDUSS/"
                        "BAIDU_STOKEN đã hết hạn."
                    ),
                    "errno": errno,
                    "errmsg": errmsg,
                },
            )
            sys.exit(1)

        api.transfer_shared_paths(remotedir=dest_dir, shared_paths=shared_paths)

        saved_paths = [p for p in (_entry_path(e) for e in shared_paths) if p]
        logger.info("Transfer thành công — %d path đã lưu vào %s", len(saved_paths), dest_dir)

        _send_callback(
            callback_url, webhook_secret, job_id,
            {
                "status": "success",
                "dest_dir": dest_dir,
                "saved_paths": saved_paths,
            },
        )

    except Exception as exc:  # noqa: BLE001
        errno, errmsg = _extract_errno_errmsg(exc)
        logger.exception("Resolve/transfer thất bại")
        _send_callback(
            callback_url, webhook_secret, job_id,
            {
                "status": "error",
                "error_message": str(exc),
                "errno": errno,
                "errmsg": errmsg,
            },
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
