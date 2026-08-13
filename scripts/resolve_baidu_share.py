#!/usr/bin/env python3
"""
Chạy TRONG GitHub Actions (không chạy trên HF Space).

Nhiệm vụ: dùng Python SDK `baidupcs_py.baidupcs.BaiduPCSApi` để verify
passcode + resolve link share Baidu Pan rồi transfer thẳng vào Cloud cá
nhân, sau đó POST kết quả về webhook của HF Space.

--- QUAY LẠI SDK, BỎ HẲN CLI/SUBPROCESS ---
Đã thử dùng CLI `BaiduPCS-Py` qua subprocess (bản trước của file này) và
gặp hàng loạt vấn đề prompt tương tác không tài liệu hoá:
  - `useradd` hỏi thêm "Account Name []: " dù đã có --cookies/--bduss.
  - Khi KHÔNG cấp stdin: click gặp EOF ngay tại prompt -> "Aborted!".
  - Khi CÓ cấp stdin (vài dòng newline rỗng): lệnh chạy xong với return
    code 1, không in gì thêm, không traceback -> hành vi không đoán được,
    không có cách nào chắc chắn debug tiếp mà không có source code CLI
    trong tay.
CLI này rõ ràng được thiết kế để dùng tương tác (con người ngồi gõ), không
phải cho automation/CI. Card đáng tin cậy hơn cho GitHub Actions là gọi
thẳng Python SDK — không có prompt, lỗi trả về là Python exception rõ
ràng, dễ log/debug.

--- ĐÂY MỚI LÀ ĐIỂM MẤU CHỐT SỬA LỖI GỐC ---
Bug đầu tiên được báo cáo: `api.shared_paths()` luôn trả về RỖNG với link
có passcode, vì "chưa verify Passcode (thiếu cookie phiên BDCLND)". README
chính thức của BaiduPCS-Py liệt kê `BaiduPCSApi.access_shared` là một API
method có thật (trong danh sách "api không thread-safe"). Đây chính là
hàm để verify passcode + set cookie BDCLND — bước đã bị BỎ SÓT ở mọi lần
thử trước (kể cả bản gốc ban đầu chỉ gọi thẳng `shared_paths()`).

Do README không công bố rõ chữ ký tham số đầy đủ của `access_shared()`,
hàm `_call_access_shared()` bên dưới tự dò chữ ký bằng `inspect.signature`
rồi gọi bằng đúng tên tham số của bản đang cài, thay vì đoán cứng cú pháp
(tránh lặp lại kiểu lỗi "đoán sai cú pháp" đã gặp nhiều lần với CLI).

shared_paths() và transfer_shared_paths() PHẢI chạy trong CÙNG 1 process
vì kết quả của shared_paths() là các object Python (mang uk/shareid)
không thể serialize qua JSON để dùng lại ở process khác — đây là lý do
toàn bộ bước "transfer vào cloud" phải nằm gọn trong GitHub Actions.
"""

from __future__ import annotations

import inspect
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
    trong CÙNG 1 lần chạy. Giá trị nhạy cảm chỉ log ĐỘ DÀI, không log giá
    trị thật."""
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
    """Ghép passcode vào URL dưới dạng query string `?pwd=...`. GIỮ HÀM NÀY
    làm lớp phòng hờ vô hại — một số version/route nội bộ của thư viện có
    thể đọc pwd từ URL — nhưng KHÔNG còn là cơ chế verify chính (đó là việc
    của `access_shared()` bên dưới). Nếu URL đã có sẵn `?pwd=...` thì GIỮ
    NGUYÊN, không ghi đè."""
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


def _call_access_shared(api: BaiduPCSApi, share_url: str, passcode: str) -> Any:
    """Gọi `api.access_shared()` để verify passcode + set cookie BDCLND —
    ĐÂY LÀ BƯỚC BỊ THIẾU khiến `shared_paths()` luôn trả về rỗng với link
    có passcode ở MỌI lần thử trước đó.

    README chính thức chỉ xác nhận method này TỒN TẠI (trong danh sách "api
    không thread-safe"), không công bố chữ ký tham số đầy đủ. Thay vì đoán
    cứng, hàm này tự dò chữ ký bằng `inspect.signature` rồi map tham số URL
    và password theo TÊN thường gặp, chỉ fallback sang gọi theo VỊ TRÍ
    (url, password) nếu không dò được — đúng thứ tự y hệt CLI:
    `save SHARED_URL DEST_DIR -p PASSWORD`.
    """
    if not passcode:
        return None

    access_fn = getattr(api, "access_shared", None)
    if not callable(access_fn):
        logger.warning(
            "Bản baidupcs-py đang cài KHÔNG có access_shared() — bỏ qua "
            "bước verify passcode tường minh, để shared_paths() tự xử lý "
            "(nhiều khả năng sẽ trả rỗng nếu link có passcode).",
        )
        return None

    try:
        sig = inspect.signature(access_fn)
        param_names = [
            p.name for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
    except (TypeError, ValueError):
        param_names = []

    kwargs: dict[str, str] = {}
    for cand in ("shared_url", "share_url", "url"):
        if cand in param_names:
            kwargs[cand] = share_url
            break
    for cand in ("password", "passwd", "pwd", "passcode"):
        if cand in param_names:
            kwargs[cand] = passcode
            break

    logger.debug(
        "access_shared() có tham số: %s — sẽ gọi với kwargs keys=%s",
        param_names, list(kwargs.keys()),
    )

    if len(kwargs) == 2:
        return access_fn(**kwargs)

    logger.debug(
        "Không map được đủ 2 tham số qua introspection — fallback gọi "
        "theo vị trí access_shared(share_url, passcode).",
    )
    return access_fn(share_url, passcode)


class BaiduDownloadError(Exception):
    """Lỗi phát sinh khi resolve/transfer share Baidu — raise với message rõ
    ràng để callback trả về error_message hữu ích cho tầng gọi (HF Space)."""


def _call_transfer_shared_paths(
    api: BaiduPCSApi, dest_dir: str, entries: list[Any], share_url: str,
) -> Any:
    """Gọi `api.transfer_shared_paths()` với CHỮ KÝ THẬT — đã xác nhận qua
    TypeError thực tế trên production (KHÔNG phải `(remotedir, *shared_paths)`
    như suy đoán ở bản trước, cũng KHÔNG phải `(remotedir=, shared_paths=)`):

        transfer_shared_paths(self, remotedir, uk, share_id, bdstoken,
                               shared_url, *fs_ids)

    `uk`/`share_id`/`bdstoken` KHÔNG tự suy ra được — chúng lấy trực tiếp từ
    field cùng tên có sẵn trên mỗi `PcsSharedPath` (kết quả của
    `shared_paths()`, xem RAW log). Cả 3 giá trị này GIỐNG NHAU cho mọi entry
    trong cùng 1 link share (đều mô tả CHÍNH link share đó, không phải riêng
    từng file/thư mục), nên chỉ cần lấy từ entry đầu tiên. Phần biến đổi theo
    từng entry là `fs_id` — được truyền RỜI TỪNG CÁI ở cuối qua `*fs_ids`.

    `shared_url` truyền vào đây PHẢI là share_url gốc (giữ nguyên `?pwd=...`
    nếu có) — cùng giá trị đã dùng để gọi `shared_paths()`.
    """
    if not entries:
        raise BaiduDownloadError("Không có entry nào để transfer (danh sách rỗng).")

    first = entries[0]
    uk = getattr(first, "uk", None)
    share_id = getattr(first, "share_id", None)
    bdstoken = getattr(first, "bdstoken", None)
    fs_ids = [getattr(e, "fs_id", None) for e in entries]
    fs_ids = [f for f in fs_ids if f is not None]

    missing = [
        name for name, val in (
            ("uk", uk), ("share_id", share_id), ("bdstoken", bdstoken),
        ) if val is None
    ] + (["fs_id (mọi entry đều thiếu)"] if not fs_ids else [])
    if missing:
        raise BaiduDownloadError(
            "Entry trả về từ shared_paths() thiếu field cần thiết để gọi "
            f"transfer_shared_paths(): {', '.join(missing)} — RAW entry đầu "
            f"tiên: {first!r}"
        )

    logger.debug(
        "Gọi transfer_shared_paths(remotedir=%r, uk=%r, share_id=%r, "
        "bdstoken=%r..., shared_url=%r, fs_ids=%r)",
        dest_dir, uk, share_id,
        (bdstoken[:8] + "...") if isinstance(bdstoken, str) else bdstoken,
        share_url, fs_ids,
    )
    return api.transfer_shared_paths(dest_dir, uk, share_id, bdstoken, share_url, *fs_ids)


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
        # BƯỚC 1 (nếu có passcode) — access_shared() để verify passcode và
        # set cookie BDCLND. THIẾU bước này là lý do gốc shared_paths()
        # luôn trả về rỗng ở mọi lần thử trước.
        # ------------------------------------------------------------- #
        if passcode:
            logger.info("Verify passcode qua access_shared() (set cookie BDCLND)...")
            try:
                access_result = _call_access_shared(api, share_url, passcode)
                logger.debug("RAW access_shared(): %r", access_result)
            except Exception as exc:  # noqa: BLE001
                errno, errmsg = _extract_errno_errmsg(exc)
                logger.error(
                    "access_shared(%s) raise exception — errno=%s errmsg=%s raw=%r",
                    share_url, errno, errmsg, exc,
                )
                if _looks_like_captcha_or_cookie_issue(exc):
                    raise RuntimeError(
                        f"access_shared() lỗi nghi do sai passcode, CAPTCHA/mã "
                        f"xác minh, hoặc Cookie BDUSS/STOKEN đã hết hạn: {exc}"
                    ) from exc
                raise
        else:
            logger.info("Link không có passcode — bỏ qua bước access_shared().")

        # ------------------------------------------------------------- #
        # BƯỚC 2 — shared_paths(). Sau khi access_shared() đã set cookie
        # BDCLND (nếu có passcode), lệnh này giờ mới có thể trả đúng danh
        # sách file thay vì rỗng.
        # ------------------------------------------------------------- #
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
                        "Danh sách file rỗng dù đã gọi access_shared() — link có "
                        "thể đã die, bị gỡ, sai passcode, hoặc Cookie "
                        "BAIDU_BDUSS/BAIDU_STOKEN đã hết hạn."
                    ),
                    "errno": errno,
                    "errmsg": errmsg,
                },
            )
            sys.exit(1)

        # ------------------------------------------------------------- #
        # BƯỚC 3 — transfer_shared_paths() PHẢI chạy cùng process với
        # shared_paths() ở trên (object Python mang uk/shareid, không
        # serialize qua JSON được). Xem docstring `_call_transfer_shared_paths`
        # để biết chữ ký thật đã xác nhận qua lỗi thực tế trên production.
        # ------------------------------------------------------------- #
        _call_transfer_shared_paths(api, dest_dir, list(shared_paths), share_url)

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
