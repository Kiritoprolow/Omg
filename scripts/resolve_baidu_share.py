#!/usr/bin/env python3
"""
Chạy TRONG GitHub Actions (không chạy trên HF Space).

Nhiệm vụ: gọi CLI `BaiduPCS-Py` (qua subprocess, KHÔNG dùng Python SDK
`baidupcs_py.baidupcs.BaiduPCSApi` nữa) để verify passcode + resolve link
share Baidu Pan rồi transfer thẳng vào Cloud cá nhân, sau đó POST kết quả
(thành công kèm output CLI, hoặc lỗi kèm output CLI để debug) về webhook
của HF Space.

--- TẠI SAO CHUYỂN SANG SUBPROCESS + CLI (thay vì Python SDK) ---
Bản `baidupcs-py` đang cài KHÔNG có `BaiduPCSApi.save_shared()`, và
`BaiduPCSApi.shared_paths()` cấp thấp không tự verify passcode (luôn trả
về rỗng nếu link có mật khẩu). CLI `BaiduPCS-Py save` verify passcode NGẦM
bên trong nên đây là cách chắc chắn nhất, đúng như README chính thức của
PeterDing/BaiduPCS-Py mô tả.

--- CÁC LỖI ĐÃ GẶP TRƯỚC ĐÓ VÀ LÝ DO (đối chiếu với README chính thức) ---
1. `subprocess.run(["baidupcs-py", ...])` → FileNotFoundError.
   Lý do: tên lệnh viết HOA/thường đúng là `BaiduPCS-Py`, không phải
   `baidupcs-py` viết thường toàn bộ.
2. `python -m baidupcs_py` → No module named baidupcs_py.__main__.
   Lý do: gói này không có `__main__.py`, chỉ có console-script entry
   point tên `BaiduPCS-Py` do pip cài vào thư mục bin.
3. `BaiduPCS-Py login` → Error: No command: login.
   Lý do: KHÔNG có lệnh `login`. Lệnh đúng để nạp cookie/bduss là
   `useradd`.
4. `BaiduPCS-Py --bduss=...` → Error: No such option '--bduss'.
   Lý do: `--bduss` là option của SUB-COMMAND `useradd`
   (`BaiduPCS-Py useradd --bduss=...`), không phải option cấp cao nhất
   của chương trình.

--- ĐIỂM QUAN TRỌNG DỄ BỎ SÓT ---
Theo README: nếu chỉ thêm `--bduss` mà KHÔNG có `--cookies` chứa giá trị
STOKEN, tài khoản đó sẽ KHÔNG dùng được lệnh `share`/`save` để lưu link
người khác chia sẻ. Vì vậy script này LUÔN ghép STOKEN vào chuỗi
`--cookies` (không chỉ truyền `--bduss` riêng lẻ).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import site
import subprocess
import sys
from typing import Any

import requests

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


def _find_cli() -> str:
    """Dò tìm đường dẫn file thực thi CLI `BaiduPCS-Py` (CHÚ Ý: viết đúng
    HOA/thường, không phải `baidupcs-py`).

    Thử theo thứ tự, dừng ở bước đầu tiên tìm thấy:
    1. `shutil.which("BaiduPCS-Py")` — trường hợp thư mục Scripts/bin của
       Python hiện tại đã nằm trong PATH.
    2. Cùng thư mục với `sys.executable` — đây là nơi pip cài entry point
       console-script của đúng bản Python đang chạy script này. Cách này
       ỔN ĐỊNH HƠN hardcode đường dẫn kiểu
       `/opt/hostedtoolcache/Python/3.10.20/x64/bin/BaiduPCS-Py` (đã dò
       thủ công trước đó), vì số phiên bản patch Python (`3.10.20`) có thể
       đổi bất kỳ lúc nào khi GitHub cập nhật runner image, trong khi
       `os.path.dirname(sys.executable)` luôn tự trỏ đúng.
    3. Thư mục bin trong `site.getuserbase()` — phòng trường hợp cài bằng
       `pip install --user`.
    """
    which = shutil.which("BaiduPCS-Py")
    if which:
        logger.debug("Tìm thấy CLI qua PATH: %s", which)
        return which

    candidate = os.path.join(os.path.dirname(sys.executable), "BaiduPCS-Py")
    if os.path.isfile(candidate):
        logger.debug("Tìm thấy CLI cùng thư mục với sys.executable: %s", candidate)
        return candidate

    try:
        user_candidate = os.path.join(site.getuserbase(), "bin", "BaiduPCS-Py")
        if os.path.isfile(user_candidate):
            logger.debug("Tìm thấy CLI trong user base: %s", user_candidate)
            return user_candidate
    except Exception:  # noqa: BLE001
        pass

    logger.error(
        "Không tìm thấy file thực thi CLI `BaiduPCS-Py`. Đã thử: PATH, %s",
        candidate,
    )
    sys.exit(1)


def _run_cli(
    cli_path: str,
    args: list[str],
    *,
    mask: set[str] | None = None,
    timeout: int = 120,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    """Chạy `cli_path` với `args`, log lại command đã chạy (che các giá trị
    nhạy cảm liệt kê trong `mask` khi in log — KHÔNG ảnh hưởng tới lệnh
    thực thi thật). Luôn trả về CompletedProcess kể cả khi return code != 0
    (không dùng check=True) để _main_ tự quyết định cách raise lỗi.

    `input_text`: nội dung ghi vào stdin của tiến trình con. CẦN THIẾT cho
    những lệnh của `BaiduPCS-Py` có prompt tương tác dạng "Xxx []: " với
    default RỖNG (VD lệnh `useradd` luôn hỏi "Account Name []: " dù đã
    truyền đủ --cookies/--bduss, và option này không được liệt kê trong
    README). Trên GitHub Actions, stdin của job KHÔNG phải tty và KHÔNG có
    dữ liệu — nếu không cấp `input_text`, `click` gặp EOF ngay tại prompt
    và thoát với "Aborted!" thay vì tự nhận default. Truyền vài dòng
    newline rỗng để "bấm Enter" qua các prompt kiểu này, chấp nhận default
    hiển thị trong `[]`.

    LƯU Ý BẢO MẬT: BDUSS/passcode được truyền qua argv nên về lý thuyết có
    thể thấy được qua `ps aux` trong lúc lệnh đang chạy. Chấp nhận được vì
    GitHub Actions runner là VM dùng riêng cho 1 job rồi huỷ ngay, không có
    tiến trình lạ nào khác cùng chạy trên máy để dòm ps list.
    """
    display_args = [("***" if a in mask else a) for a in args] if mask else args
    logger.info("Chạy lệnh: %s %s", cli_path, " ".join(display_args))

    try:
        result = subprocess.run(
            [cli_path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Lệnh timeout sau %ss: %s", timeout, exc)
        raise RuntimeError(
            f"CLI timeout sau {timeout}s khi chạy `{args[0] if args else ''}`"
        ) from exc

    logger.debug("Return code: %s", result.returncode)
    logger.debug("STDOUT:\n%s", result.stdout)
    logger.debug("STDERR:\n%s", result.stderr)
    return result


def _looks_like_captcha_or_cookie_issue(text: Any) -> bool:
    """Nhận diện sơ bộ (dựa trên keyword) xem output/lỗi có khả năng do
    CAPTCHA/mã xác minh (vcode) hoặc Cookie (BDUSS/STOKEN) đã hết hạn hay
    không — giúp error_message trả về HF Space đủ cụ thể để không phải
    đoán mò."""
    lowered = str(text).lower()
    keywords = (
        "vcode", "captcha", "verify", "验证码", "登录", "login",
        "unauthorized", "-6", "cookie", "bduss", "stoken",
    )
    return any(kw in lowered for kw in keywords)


def _looks_like_prompt_eof_issue(text: Any) -> bool:
    """Nhận diện trường hợp CLI abort do gặp EOF trên stdin không tương tác
    tại một prompt tương tác nào đó (VD "Account Name []: " của `useradd`)
    — khác với lỗi captcha/cookie, cần hint riêng để không đoán nhầm hướng
    debug (không phải do Cookie sai, mà do thiếu `input_text` cấp cho
    prompt đó)."""
    lowered = str(text).lower()
    return "abort" in lowered


def _extract_errno_errmsg_from_text(text: str) -> tuple[str | None, str | None]:
    """Bóc errno/errmsg (nếu có) từ output text của CLI — dùng để debug khi
    `save` thất bại, output CLI thường in kèm errno/errmsg gốc từ Baidu."""
    m_errno = re.search(r"errno[\"']?\s*[:=]\s*(-?\d+)", text, re.IGNORECASE)
    m_errmsg = re.search(r"err(?:_)?msg[\"']?\s*[:=]\s*[\"']?([^\"',}\n]+)", text, re.IGNORECASE)
    errno = m_errno.group(1) if m_errno else None
    errmsg = m_errmsg.group(1).strip() if m_errmsg else None
    return errno, errmsg


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

    cli_path = _find_cli()
    logger.info("Dùng CLI tại: %s", cli_path)

    try:
        # ------------------------------------------------------------- #
        # BƯỚC 1 — `useradd` KHÔNG TƯƠNG TÁC. Ghép STOKEN vào --cookies vì
        # theo README chính thức, nếu cookies không có STOKEN thì tài
        # khoản đó không dùng được lệnh `save` để lưu link người khác chia
        # sẻ (chỉ có --bduss riêng lẻ là không đủ).
        # ------------------------------------------------------------- #
        cookies_str = f"BDUSS={bduss};"
        if stoken:
            cookies_str += f" STOKEN={stoken};"
        else:
            logger.warning(
                "Không có BAIDU_STOKEN — theo README, thiếu STOKEN trong "
                "cookies sẽ khiến lệnh `save` không dùng được. Vẫn thử "
                "chạy để log lỗi cụ thể nếu có.",
            )

        useradd_result = _run_cli(
            cli_path,
            ["useradd", "--cookies", cookies_str, "--bduss", bduss],
            mask={cookies_str, bduss},
            timeout=60,
            # `useradd` luôn hỏi thêm "Account Name []: " (không nằm trong
            # README) dù đã có --cookies/--bduss. Cấp sẵn vài dòng rỗng để
            # tự nhận default "" qua prompt này (và các prompt tương tự nếu
            # có), tránh bị "Aborted!" do EOF trên stdin không tương tác.
            input_text="\n" * 5,
        )
        if useradd_result.returncode != 0:
            combined = f"{useradd_result.stdout}\n{useradd_result.stderr}".strip()
            if _looks_like_prompt_eof_issue(combined):
                hint = (
                    " (nghi do CLI có thêm prompt tương tác chưa được cấp "
                    "input_text — không phải lỗi Cookie)"
                )
            elif _looks_like_captcha_or_cookie_issue(combined):
                hint = " (nghi do Cookie BDUSS/STOKEN đã hết hạn hoặc sai)"
            else:
                hint = ""
            raise RuntimeError(f"useradd thất bại{hint}: {combined[-1000:]}")

        logger.info("useradd thành công — tài khoản vừa thêm tự động là tài khoản hiện hành.")

        # ------------------------------------------------------------- #
        # BƯỚC 2 (best-effort) — tạo trước DEST_DIR trên Baidu Cloud. Bỏ
        # qua lỗi nếu thư mục đã tồn tại; không ảnh hưởng tới bước `save`
        # phía dưới dù bước này có fail vì lý do gì khác.
        # ------------------------------------------------------------- #
        mkdir_result = _run_cli(cli_path, ["mkdir", dest_dir], timeout=60, input_text="\n")
        if mkdir_result.returncode != 0:
            logger.debug(
                "mkdir %s trả về lỗi (bỏ qua, có thể do đã tồn tại sẵn): %s",
                dest_dir, (mkdir_result.stdout + mkdir_result.stderr).strip()[-500:],
            )

        # ------------------------------------------------------------- #
        # BƯỚC 3 — `save SHARE_URL DEST_DIR -p PASSCODE --no-show-vcode`.
        # `--no-show-vcode` BẮT BUỘC trong môi trường non-interactive: nếu
        # Baidu yêu cầu mã xác minh (vcode) mà không có cờ này, CLI sẽ cố
        # hỏi nhập tương tác → treo cho tới khi hết `timeout-minutes` của
        # job, thay vì trả lỗi ngay để mình debug được.
        # ------------------------------------------------------------- #
        save_args = ["save", share_url, dest_dir]
        if passcode:
            save_args += ["-p", passcode]
        save_args.append("--no-show-vcode")

        save_result = _run_cli(
            cli_path, save_args,
            mask={passcode} if passcode else None,
            timeout=480,
            # Đề phòng có prompt phụ nào đó chưa lường trước; --no-show-vcode
            # đã lo phần vcode nên đây chỉ là lớp phòng hờ, vô hại nếu không
            # có prompt nào thật sự đọc tới các dòng rỗng này.
            input_text="\n\n",
        )
        combined_out = f"{save_result.stdout}\n{save_result.stderr}".strip()

        if save_result.returncode != 0:
            errno, errmsg = _extract_errno_errmsg_from_text(combined_out)
            hint = (
                " (nghi do sai passcode, cần mã xác minh/vcode, hoặc Cookie "
                "BDUSS/STOKEN hết hạn)"
                if _looks_like_captcha_or_cookie_issue(combined_out) else ""
            )
            logger.error(
                "save thất bại — errno=%s errmsg=%s output=%s",
                errno, errmsg, combined_out[-1500:],
            )
            _send_callback(
                callback_url, webhook_secret, job_id,
                {
                    "status": "error",
                    "error_message": f"save thất bại{hint}: {combined_out[-1500:]}",
                    "errno": errno,
                    "errmsg": errmsg,
                },
            )
            sys.exit(1)

        logger.info("save thành công vào %s", dest_dir)
        logger.debug("save output:\n%s", combined_out)

        _send_callback(
            callback_url, webhook_secret, job_id,
            {
                "status": "success",
                "dest_dir": dest_dir,
                "cli_output": combined_out[-2000:],
            },
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Resolve/transfer thất bại")
        _send_callback(
            callback_url, webhook_secret, job_id,
            {
                "status": "error",
                "error_message": str(exc),
            },
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
