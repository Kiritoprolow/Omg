import os
import sys
import json
import logging
import shutil
import subprocess
import requests

# Cấu hình log
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

def find_baidupcs_bin() -> str:
    """Tự động dò tìm file thực thi CLI của baidupcs-py"""
    py_bin_dir = os.path.dirname(sys.executable)
    candidates = ["BaiduPCS-Py", "baidupcs-py", "baidupcs", "baidupcs_py"]

    # 1. Kiểm tra ngay trong thư mục bin của Python hiện tại
    for name in candidates:
        full_path = os.path.join(py_bin_dir, name)
        if os.path.exists(full_path):
            logging.info(f"Đã tìm thấy CLI executable tại: {full_path}")
            return full_path

    # 2. Kiểm tra trong PATH hệ thống
    for name in candidates:
        found = shutil.which(name)
        if found:
            logging.info(f"Đã tìm thấy CLI executable qua PATH: {found}")
            return found

    return "BaiduPCS-Py"

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

    # Tìm file thực thi CLI
    baidupcs_cmd = find_baidupcs_bin()

    # 2. Tạo câu lệnh save trực tiếp kèm Cookie BDUSS/STOKEN toàn cục
    save_cmd = [baidupcs_cmd]
    if bduss:
        save_cmd.extend(["--bduss", bduss])
    if stoken:
        save_cmd.extend(["--stoken", stoken])

    save_cmd.extend(["save", share_url, dest_dir])
    if passcode:
        save_cmd.extend(["-p", passcode])

    logging.info(f"Thực thi lệnh BaiduPCS-Py save...")
    save_res = subprocess.run(save_cmd, capture_output=True, text=True)

    output_log = (save_res.stdout or "") + "\n" + (save_res.stderr or "")
    logging.debug(f"RAW Output từ BaiduPCS-Py:\n{output_log}")

    # 3. Kiểm tra kết quả và gửi Webhook trả lời HF Space
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
