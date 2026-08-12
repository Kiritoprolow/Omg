import os
import sys
import json
import logging
import subprocess
import requests

# Cấu hình log
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def send_callback(callback_url: str, payload: dict, secret: str = None):
    """Gửi HTTP POST webhook callback về HF Space"""
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-Webhook-Secret"] = secret
    
    try:
        logging.info(f"Gửi callback về {callback_url}...")
        resp = requests.post(callback_url, json=payload, headers=headers, timeout=15)
        logging.info(f"Callback hoàn tất (HTTP {resp.status_code})")
    except Exception as e:
        logging.error(f"Lỗi gửi callback: {e}")

def main():
    # 1. Đọc biến môi trường do GitHub Actions cấp
    job_id = os.getenv("JOB_ID")
    share_url = os.getenv("SHARE_URL")
    passcode = os.getenv("PASSCODE", "").strip()
    dest_dir = os.getenv("DEST_DIR", "/app_temp_download")
    callback_url = os.getenv("CALLBACK_URL")
    
    bduss = os.getenv("BAIDU_BDUSS")
    stoken = os.getenv("BAIDU_STOKEN", "")
    webhook_secret = os.getenv("BAIDU_WEBHOOK_SECRET")

    logging.debug(f"JOB_ID: {job_id}")
    logging.debug(f"SHARE_URL: {share_url}")
    logging.debug(f"PASSCODE: {passcode}")
    logging.debug(f"DEST_DIR: {dest_dir}")

    # Validate thông tin cơ bản
    if not job_id or not share_url or not callback_url:
        logging.error("Thiếu biến môi trường bắt buộc (JOB_ID, SHARE_URL, CALLBACK_URL)")
        sys.exit(1)

    if not bduss:
        err_msg = "Thiếu cookie BAIDU_BDUSS trên GitHub Secrets"
        send_callback(callback_url, {"job_id": job_id, "status": "error", "error_message": err_msg}, webhook_secret)
        sys.exit(1)

    # 2. Đăng nhập tài khoản Baidu qua module Python (-m baidupcs_py)
    login_cmd = [sys.executable, "-m", "baidupcs_py", "login", f"--bduss={bduss}"]
    if stoken:
        login_cmd.append(f"--stoken={stoken}")

    logging.info("Đang đăng nhập Baidu qua CLI...")
    login_res = subprocess.run(login_cmd, capture_output=True, text=True)
    if login_res.returncode != 0:
        err_msg = f"Đăng nhập Baidu CLI thất bại: {login_res.stderr or login_res.stdout}"
        logging.error(err_msg)
        send_callback(callback_url, {"job_id": job_id, "status": "error", "error_message": err_msg}, webhook_secret)
        sys.exit(1)

    # 3. Gọi lệnh save để lưu file từ link share
    save_cmd = [sys.executable, "-m", "baidupcs_py", "save", share_url, dest_dir]
    if passcode:
        save_cmd.extend(["-p", passcode])

    logging.info(f"Thực thi lệnh save: {' '.join(save_cmd)}")
    save_res = subprocess.run(save_cmd, capture_output=True, text=True)

    output_log = (save_res.stdout or "") + "\n" + (save_res.stderr or "")
    logging.debug(f"RAW Output từ baidupcs-py:\n{output_log}")

    # 4. Kiểm tra kết quả và gửi Webhook trả lời HF Space
    if save_res.returncode == 0:
        logging.info("Lưu file Baidu Share thành công!")
        send_callback(callback_url, {
            "job_id": job_id,
            "status": "success",
            "dest_dir": dest_dir,
            "message": "Đã chuyển file từ link chia sẻ vào Cloud Baidu thành công!"
        }, webhook_secret)
    else:
        err_msg = f"Lưu file thất bại (Exit code {save_res.returncode}): {output_log.strip()}"
        logging.error(err_msg)
        send_callback(callback_url, {
            "job_id": job_id,
            "status": "error",
            "error_message": err_msg
        }, webhook_secret)
        sys.exit(1)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        job_id = os.getenv("JOB_ID", "unknown")
        callback_url = os.getenv("CALLBACK_URL")
        webhook_secret = os.getenv("BAIDU_WEBHOOK_SECRET")
        
        logging.exception("Fatal Unhandled Exception")
        if callback_url:
            send_callback(callback_url, {
                "job_id": job_id,
                "status": "error",
                "error_message": f"Exception hệ thống: {str(e)}"
            }, webhook_secret)
        sys.exit(1)
