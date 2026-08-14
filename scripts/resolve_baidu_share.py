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

import hashlib
import inspect
import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import requests
from baidupcs_py.baidupcs import BaiduPCSApi

# BaiduPCSError — exception gốc mà baidupcs-py raise ra cho MỌI lỗi API trả
# về (errno/error_code != 0, vd: error_code=4 "存储好像出问题了" — kho lưu
# trữ tạm thời gặp sự cố phía Baidu). Vị trí import thay đổi tuỳ version thư
# viện nên thử lần lượt vài chỗ phổ biến nhất; nếu không tìm thấy ở đâu cả,
# fallback về 1 class rỗng kế thừa Exception để `except (..., BaiduPCSError)`
# vẫn chạy được (không isinstance-match được gì thêm ngoài các nhánh check
# errno/message thủ công vốn đã có sẵn bên dưới) thay vì crash ngay lúc import.
try:
    from baidupcs_py.common.errors import BaiduPCSError
except ImportError:
    try:
        from baidupcs_py.baidupcs.errors import BaiduPCSError  # type: ignore[no-redef]
    except ImportError:
        try:
            from baidupcs_py.errors import BaiduPCSError  # type: ignore[no-redef]
        except ImportError:
            class BaiduPCSError(Exception):  # type: ignore[no-redef]
                """Fallback rỗng khi không tìm thấy BaiduPCSError thật ở bất kỳ
                vị trí import nào đã thử — xem comment phía trên."""

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


def _entry_is_dir(entry: Any) -> bool:
    if isinstance(entry, dict):
        return bool(entry.get("isdir") or entry.get("is_dir"))
    is_dir = getattr(entry, "is_dir", None)
    if is_dir is not None:
        return bool(is_dir() if callable(is_dir) else is_dir)
    return bool(getattr(entry, "isdir", False))


def _entry_fs_id(entry: Any) -> Any:
    if isinstance(entry, dict):
        return entry.get("fs_id")
    return getattr(entry, "fs_id", None)


def _entry_filename(entry: Any) -> str | None:
    """Lấy NGUYÊN VĂN tên thư mục/file (KHÔNG qua Regex/tách số) — ưu tiên
    field `server_filename` (tên Baidu trả về, đúng y hệt tên hiển thị trên
    Cloud), fallback `filename`, rồi mới fallback về tên cuối cùng của
    `path`/`remotepath` (`Path(...).name`) nếu entry không có sẵn 2 field
    trên. Dùng làm `drama_id` — KHÔNG được dùng Regex tách số ở đầu tên
    thư mục để làm id, vì tên folder trên thực tế bị đánh số lộn xộn (VD:
    thư mục "217" xuất hiện ở vị trí thứ 15 trong danh sách) khiến số bị
    tách ra không phản ánh đúng danh tính bộ phim — phải giữ NGUYÊN chuỗi
    tên gốc để so khớp/dedup chính xác 1-1 với `PROCESSED_DRAMA_IDS`."""
    if isinstance(entry, dict):
        name = entry.get("server_filename") or entry.get("filename")
    else:
        name = getattr(entry, "server_filename", None) or getattr(entry, "filename", None)
    if name:
        return str(name)

    path = _entry_path(entry)
    return Path(path).name if path else None


# Chặn đệ quy vô hạn nếu Baidu trả cấu trúc lồng nhau lỗi/vòng lặp khi đào sâu
# vào 1 Folder bộ phim con của link share (khác BAIDU_MAX_SCAN_DEPTH bên
# baidu_downloader.py — đó là quét CÂY THƯ MỤC RIÊNG trên Cloud cá nhân SAU
# transfer, còn hằng số này là quét TRỰC TIẾP bên trong link share CỦA NGƯỜI
# KHÁC TRƯỚC khi transfer).
MAX_SHARE_SUBFOLDER_DEPTH = 8

# HARD CAP an toàn (chốt chặn lớp 2 chống "cào tràn" — vụ thực tế: bug
# list_shared_paths() của baidupcs-py đôi khi BỎ QUA sub-path truyền vào và
# tự động trả về CẢ ROOT SHARE (~4.131 file/561 batch) khi path bị lệch định
# dạng — không phụ thuộc baidupcs-py, đây là chốt ở tầng code của chính ta).
# 1 bộ phim chuẩn chỉ có 50-150 tập (~5-15 batch) — KHÔNG BAO GIỜ vượt quá
# ngưỡng này; nếu vượt, gần như chắc chắn đã quét lọt ra ngoài Folder đã
# chọn dù đã lọc theo target_folder_name, nên phải cắt đuôi cứng.
BAIDU_MAX_MP4_PER_DRAMA = 200

_EPISODE_NUMBER_RE = re.compile(r"(\d+)")


def _natural_sort_key(entry: Any) -> tuple:
    """Trích số tập bằng Regex từ TÊN FILE (không phải cả path) để sắp xếp
    tăng dần đúng thứ tự tập (1, 2, 3... chứ không phải sort chuỗi kiểu "1,
    10, 2, 3..."). Entry không tách được số tập bị đẩy xuống CUỐI danh sách."""
    name = Path(_entry_path(entry) or "").name
    numbers = [int(n) for n in _EPISODE_NUMBER_RE.findall(name)]
    has_number = 1 if numbers else 0
    return (1 - has_number, numbers, name)


def _derive_flat_drama_id(share_url: str) -> str:
    """Sinh 1 drama_id ỔN ĐỊNH cho trường hợp link share KHÔNG có Folder bộ
    phim con (bản thân link share đã là 1 bộ phim, file .mp4 nằm ngay cấp
    gốc). Giữ đồng bộ với `_derive_flat_drama_id` bên baidu_downloader.py —
    CÙNG công thức để `processed_drama_ids` do HF Space gửi sang khớp đúng
    id mà job này tự tính ra."""
    clean = share_url.split("?", 1)[0]
    match = re.search(r"/s/1([A-Za-z0-9_-]+)", clean)
    if match:
        return f"flat:{match.group(1)}"
    return f"flat:{hashlib.md5(clean.encode('utf-8')).hexdigest()[:12]}"


def _pick_unprocessed_entry(
    entries: list[Any], processed_drama_ids: "set[str] | frozenset[str]",
) -> "tuple[Any, str] | tuple[None, None]":
    """Nhận danh sách entry Ở CẤP GỐC của link share, trả về
    `(entry_đã_chọn, drama_id)` — random 1 entry CHƯA có trong
    `processed_drama_ids`. Trả về `(None, None)` (KHÔNG raise) nếu mọi entry
    đều đã xử lý — tầng gọi (`main()`) tự quyết định gửi callback
    `status="all_processed"` rồi thoát êm, vì exception không serialize được
    qua webhook JSON.

    `drama_id` = NGUYÊN VĂN tên thư mục gốc (`_entry_filename` — ưu tiên
    `server_filename`/`filename`), KHÔNG qua bất kỳ Regex/tách số nào và
    KHÔNG có prefix `folder:` — so khớp trực tiếp 1-1 với chuỗi trong
    `PROCESSED_DRAMA_IDS` do HF Space gửi sang. TUYỆT ĐỐI KHÔNG tách số ở
    đầu tên thư mục để làm id — tên folder trên thực tế bị đánh số lộn xộn
    (VD: "217" nhưng lại nằm ở vị trí thứ 15 trong danh sách), tách số ra
    dùng làm id sẽ dedup SAI bộ phim."""
    candidates: list[tuple[Any, str]] = []
    for entry in entries:
        name = _entry_filename(entry)
        if not name:
            continue
        candidates.append((entry, name))

    if not candidates:
        raise BaiduDownloadError("Link share rỗng — shared_paths() không trả về entry nào.")

    unprocessed = [(e, did) for e, did in candidates if did not in processed_drama_ids]
    if not unprocessed:
        return None, None

    return random.choice(unprocessed)


def _call_list_shared_paths(
    api: BaiduPCSApi, folder_entry: Any, uk: Any, share_id: Any, bdstoken: Any, share_url: str,
) -> "list[Any] | None":
    """Gọi API liệt kê ITEM CON của 1 THƯ MỤC bên trong link share (khác
    `shared_paths()` — hàm đó CHỈ trả entry cấp gốc). README chính thức của
    baidupcs-py KHÔNG công bố rõ tên/chữ ký của method này, nên thử LẦN LƯỢT
    vài tên method hay gặp nhất trong các bản khác nhau
    (`list_shared_paths`/`shared_dir_list`/`list_shared_dir`), tự dò chữ ký
    bằng `inspect.signature` giống hệt cách `_call_access_shared()` đã làm ở
    trên, thay vì đoán cứng cú pháp.

    Trả về `None` (KHÔNG raise) nếu KHÔNG method nào gọi được — tầng gọi
    (`_collect_mp4_entries`) chịu trách nhiệm raise lỗi rõ ràng, để log phân
    biệt được "API không hỗ trợ" với "API hỗ trợ nhưng lỗi khác".
    """
    folder_path = _entry_path(folder_entry)
    if not folder_path:
        return None

    candidate_names = ("list_shared_paths", "shared_dir_list", "list_shared_dir")
    for name in candidate_names:
        fn = getattr(api, name, None)
        if not callable(fn):
            continue

        try:
            sig = inspect.signature(fn)
            param_names = [
                p.name for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            ]
        except (TypeError, ValueError):
            param_names = []

        kwargs: dict[str, Any] = {}
        for cand in ("sharedpath", "shared_path", "dir", "remotepath", "path"):
            if cand in param_names:
                kwargs[cand] = folder_path
                break
        if "uk" in param_names:
            kwargs["uk"] = uk
        for cand in ("share_id", "shareid"):
            if cand in param_names:
                kwargs[cand] = share_id
                break
        if "bdstoken" in param_names:
            kwargs["bdstoken"] = bdstoken

        try:
            if len(kwargs) >= 3:
                logger.debug("Thử api.%s(**%s)...", name, {k: v for k, v in kwargs.items()})
                result = fn(**kwargs)
            else:
                logger.debug(
                    "Không map được đủ tham số qua introspection cho api.%s() — "
                    "fallback gọi theo vị trí (sharedpath, uk, share_id, bdstoken).",
                    name,
                )
                result = fn(folder_path, uk, share_id, bdstoken)
            logger.info(
                "[BaiduPCS] api.%s('%s') OK — %d item con.",
                name, folder_path, len(result) if result else 0,
            )
            return list(result) if result else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Thử gọi api.%s(...) để đào sâu vào Folder thất bại: %s", name, exc)
            continue

    return None


def _filter_children_within_scope(children: list[Any], parent_path: "str | None") -> list[Any]:
    """CHỐT AN TOÀN QUAN TRỌNG (né bug tràn quét >6.800 file ngoài Folder đã
    chọn): 1 số bản baidupcs-py/API Baidu, khi gọi hàm liệt kê item con của
    1 Folder cụ thể (`list_shared_paths`/`shared_dir_list`/`list_shared_dir`),
    ĐÔI KHI KHÔNG lọc đúng theo tham số path truyền vào mà trả về luôn CẢ
    CÂY thư mục của TOÀN BỘ link share — khiến `_collect_mp4_entries` tưởng
    nhầm đó là item con của 1 Folder phim rồi đệ quy quét tràn lan ra ngoài
    phạm vi Folder đã chọn.

    Hàm này LỌC LẠI TRIỆT ĐỂ ngay sau khi nhận `children`: CHỈ giữ entry có
    `path` THỰC SỰ nằm trong `parent_path` (bằng chính `parent_path`, hoặc
    bắt đầu bằng `parent_path/`) — entry path KHÔNG khớp bị LOẠI BỎ + log
    WARNING. Đây là chốt chặn ở tầng code, KHÔNG phụ thuộc vào việc thư viện
    có lọc đúng hay không."""
    if not parent_path:
        return children

    prefix = parent_path.rstrip("/") + "/"
    in_scope: list[Any] = []
    out_of_scope = 0
    for child in children:
        child_path = _entry_path(child)
        if child_path and (child_path == parent_path or child_path.startswith(prefix)):
            in_scope.append(child)
        else:
            out_of_scope += 1

    if out_of_scope:
        logger.warning(
            "Đã LOẠI %d entry NẰM NGOÀI phạm vi Folder '%s' mà API liệt kê "
            "item con lỡ trả về (hành vi lạ/bug của baidupcs-py hoặc Baidu "
            "API) — CHỈ giữ lại entry thực sự nằm bên trong Folder phim đã "
            "chọn, tuyệt đối không quét tràn ra Root Share.",
            out_of_scope, parent_path,
        )
    return in_scope


def _enforce_mp4_hard_cap(mp4_entries: list[Any], drama_id: str) -> list[Any]:
    """CHỐT CHẶN LỚP 2 (Hard Cap Safety Switch) — độc lập với lớp lọc theo
    `target_folder_name` ở `_collect_mp4_entries`, phòng trường hợp filter đó
    vẫn bị lọt vì lý do nào đó (VD: `path` trả về bị cắt/encode lệch không
    còn chứa nguyên văn tên folder). 1 bộ phim chuẩn chỉ có 50-150 tập
    (~5-15 batch) — KHÔNG BAO GIỜ vượt quá `BAIDU_MAX_MP4_PER_DRAMA`. Nếu
    vượt, đây gần như chắc chắn là dấu hiệu quét tràn ra ngoài phạm vi Folder
    đã chọn (đúng kiểu lỗi ~4.131 file/561 batch đã gặp thực tế) — log ERROR
    + CẮT ĐUÔI chỉ giữ lại N file ĐẦU TIÊN, TUYỆT ĐỐI KHÔNG để lọt hàng nghìn
    file vào vòng lặp transfer."""
    if len(mp4_entries) <= BAIDU_MAX_MP4_PER_DRAMA:
        return mp4_entries

    dropped = len(mp4_entries) - BAIDU_MAX_MP4_PER_DRAMA
    logger.error(
        "[HARD CAP] drama_id='%s' có %d file .mp4 SAU KHI LỌC — VƯỢT NGƯỠNG "
        "AN TOÀN %d file/bộ phim (dấu hiệu quét tràn ra ngoài Folder đã chọn, "
        "kiểu lỗi ~4.131 file/561 batch đã gặp thực tế, khả năng do bug "
        "list_shared_paths() của baidupcs-py bỏ qua sub-path và tự trả về cả "
        "Root Share) — CẮT ĐUÔI chỉ giữ lại %d file ĐẦU TIÊN, LOẠI BỎ %d file "
        "còn lại để chặn đứng transfer tràn.",
        drama_id, len(mp4_entries), BAIDU_MAX_MP4_PER_DRAMA,
        BAIDU_MAX_MP4_PER_DRAMA, dropped,
    )
    return mp4_entries[:BAIDU_MAX_MP4_PER_DRAMA]


def _collect_mp4_entries(
    api: BaiduPCSApi,
    folder_entry: Any,
    uk: Any,
    share_id: Any,
    bdstoken: Any,
    share_url: str,
    target_folder_name: "str | None" = None,
    _depth: int = 0,
) -> list[Any]:
    """
    BƯỚC quan trọng — Chui vào Folder bộ phim đã chọn & LỌC lấy CHỈ file
    `.mp4` lẻ (đệ quy — Folder có thể lồng nhau 2-3 cấp), để tầng gọi CHỈ
    truyền `fs_id` của các file `.mp4` này vào `transfer_shared_paths()` —
    TUYỆT ĐỐI KHÔNG dùng fs_id của thư mục mẹ, tránh Baidu trả lỗi
    `130 (转存文件数超限)`.

    `folder_entry` LUÔN LUÔN là entry Folder phim ĐÃ CHỌN (`chosen_entry`)
    hoặc 1 Folder con lồng bên trong nó qua đệ quy — hàm này TUYỆT ĐỐI
    KHÔNG được gọi với entry cấp gốc (root) của link share, để tránh quét
    tràn ra toàn bộ Root Share (đã từng gặp thực tế: quét nhầm hơn 6.800
    file thay vì chỉ file bên trong 1 Folder phim). `_filter_children_within_
    scope()` là chốt chặn thêm ở TỪNG CẤP đệ quy, phòng trường hợp API liệt
    kê item con trả nhầm entry ngoài phạm vi Folder hiện tại.

    `target_folder_name` (LỚP LỌC CỨNG THỨ 2, độc lập với `_filter_children_
    within_scope`) — NGUYÊN VĂN tên (`server_filename`) của Folder phim đã
    chọn ở cấp gốc nhất (truyền xuống KHÔNG đổi qua mọi cấp đệ quy). Chốt
    chặn `list_shared_paths()` của baidupcs-py bị bug BỎ QUA sub-path và tự
    trả về CẢ ROOT SHARE khi path truyền vào bị lệch định dạng (vụ thực tế:
    ~4.131 file/561 batch thay vì chỉ 50-150 file của 1 bộ phim) — MỌI file
    `.mp4` mà `path` của nó KHÔNG CHỨA `target_folder_name` sẽ bị SKIP NGAY
    + log WARNING, dù đã đi qua `_filter_children_within_scope` ở trên.
    """
    if _depth > MAX_SHARE_SUBFOLDER_DEPTH:
        logger.warning(
            "[BaiduPCS] Đã đào sâu quá %d cấp trong Folder share — dừng lại "
            "để tránh đệ quy vô hạn (cấu trúc share bất thường?).",
            MAX_SHARE_SUBFOLDER_DEPTH,
        )
        return []

    children = _call_list_shared_paths(api, folder_entry, uk, share_id, bdstoken, share_url)
    if children is None:
        raise BaiduDownloadError(
            "Bản baidupcs-py đang cài KHÔNG có method liệt kê item con của 1 "
            "Folder bên trong link share (đã thử list_shared_paths/"
            "shared_dir_list/list_shared_dir, không cái nào gọi được) — "
            "không thể lọc riêng file .mp4 để tránh truyền fs_id thư mục mẹ. "
            "Vui lòng `pip install -U baidupcs-py` lên bản mới nhất, hoặc "
            "kiểm tra lại tên method thật của bản đang cài."
        )

    # Chốt an toàn LỚP 1: chỉ giữ item con THỰC SỰ nằm trong folder_entry
    # hiện tại — tuyệt đối không để lọt entry ngoài phạm vi Folder phim đã
    # chọn.
    children = _filter_children_within_scope(children, _entry_path(folder_entry))

    mp4_entries: list[Any] = []
    skipped_wrong_folder = 0
    for child in children:
        if _entry_is_dir(child):
            mp4_entries.extend(
                _collect_mp4_entries(
                    api, child, uk, share_id, bdstoken, share_url,
                    target_folder_name=target_folder_name, _depth=_depth + 1,
                )
            )
            continue
        path = _entry_path(child)
        if not path or Path(path).suffix.lower() != ".mp4":
            continue
        # Chốt an toàn LỚP 2: BẮT BUỘC path của file .mp4 phải chứa NGUYÊN
        # VĂN tên Folder phim đã chọn — nếu không, đây gần như chắc chắn là
        # file bị lọt ra từ bug list_shared_paths() trả nhầm cả Root Share.
        if target_folder_name and target_folder_name not in path:
            skipped_wrong_folder += 1
            continue
        mp4_entries.append(child)

    if skipped_wrong_folder:
        logger.warning(
            "[HARD FILTER] Đã SKIP %d file .mp4 có path KHÔNG chứa tên Folder "
            "phim đã chọn ('%s') — nghi do bug list_shared_paths() trả nhầm "
            "cả Root Share thay vì đúng sub-path.",
            skipped_wrong_folder, target_folder_name,
        )

    return mp4_entries


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


# FIX (504 Gateway Timeout + JSONDecodeError): gửi TOÀN BỘ fs_ids của cả
# 1 bộ phim (có thể vài chục -> hàng trăm tập) trong 1 request
# transfer_shared_paths() DUY NHẤT khiến Baidu xử lý quá lâu -> timeout ở
# tầng gateway (504) -> trả về HTML lỗi thay vì JSON -> baidupcs-py cố
# json.loads() body đó crash với JSONDecodeError. Từ giờ CHIA NHỎ fs_ids
# thành từng batch nhỏ, gọi transfer TUẦN TỰ từng batch thay vì 1 lần.
BAIDU_TRANSFER_BATCH_SIZE = 12  # tối đa 10-15 fs_id/batch theo yêu cầu
BAIDU_TRANSFER_BATCH_SLEEP_SECONDS = 1.0  # nghỉ giữa các batch để né rate limit
BAIDU_TRANSFER_MAX_RETRIES = 3  # số lần thử tối đa/batch khi lỗi mạng/504/JSONDecodeError
# FIX (log thực tế 13/08/2026 cho thấy error_code=4 dính LIÊN TIẾP CẢ 3 lần
# thử ở NHIỀU link/batch khác nhau, khiến job chết hẳn thay vì tự hồi phục
# "sau vài giây" như kỳ vọng ban đầu): error_code=4 giờ có RIÊNG 1 ngân sách
# retry độc lập với BAIDU_TRANSFER_MAX_RETRIES (dùng cho lỗi mạng/504), có
# exponential backoff thay vì nghỉ cố định — vì thực tế cho thấy 2 giây là
# QUÁ NGẮN, sự cố lưu trữ phía Baidu nhiều khi cần vài chục giây mới hồi.
BAIDU_ERROR_CODE_4_MAX_RETRIES = 6
BAIDU_ERROR_CODE_4_BASE_SLEEP_SECONDS = 5.0  # backoff: 5s,10s,20s,40s,60s,60s
BAIDU_ERROR_CODE_4_MAX_SLEEP_SECONDS = 60.0
# Giữ hằng số cũ (không dùng nữa ở nhánh error_code=4) để không phá vỡ chỗ
# nào khác lỡ còn tham chiếu tới tên biến này.
BAIDU_ERROR_CODE_4_RETRY_SLEEP_SECONDS = BAIDU_ERROR_CODE_4_BASE_SLEEP_SECONDS

# Cụm từ nhận diện lỗi CÓ THỂ retry được (mạng chập chờn/Baidu quá tải khi
# batch vẫn còn hơi to) — KHÔNG bao gồm lỗi tham số sai/hết dung lượng/token
# hết hạn, vì retry không giải quyết được các lỗi đó (xem _classify_transfer_error).
_RETRYABLE_TRANSFER_ERROR_MARKERS = (
    "504", "gateway timeout", "jsondecodeerror", "expecting value",
    "timeout", "connection", "timed out",
)


def _is_retryable_transfer_error(exc: Exception) -> bool:
    """Nhận diện lỗi mạng/504/JSONDecodeError (tạm thời, đáng để retry) —
    khác với lỗi cấu hình/logic (sai tham số, hết dung lượng, token hết
    hạn...) mà retry không bao giờ tự hết được."""
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _RETRYABLE_TRANSFER_ERROR_MARKERS)


def _is_error_code_2(exc: Exception) -> bool:
    """Nhận diện lỗi Baidu trả về `error_code`/`errno` = 2 (`参数错误` — tham
    số không hợp lệ). Thực tế cho thấy lỗi này thường xảy ra khi 1 (vài)
    `fs_id` CỤ THỂ trong batch bị hỏng/đã bị xoá khỏi share/không còn hợp lệ
    — KHÔNG phải lỗi cấu hình của TOÀN BỘ batch. Dùng để quyết định fallback
    tách batch ra transfer từng `fs_id` lẻ, thay vì để 1 file hỏng chặn đứng
    cả batch (hoặc raise chết luôn cả quá trình)."""
    errno, _errmsg = _extract_errno_errmsg(exc)
    if errno is not None:
        try:
            if int(errno) == 2:
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc)
    if re.search(r"error_code[\"']?\s*[:=]\s*2\b", msg, re.IGNORECASE):
        return True
    if re.search(r"\berrno[\"']?\s*[:=]\s*2\b", msg, re.IGNORECASE):
        return True
    return "参数错误" in msg


def _is_error_code_4(exc: Exception) -> bool:
    """Nhận diện lỗi Baidu trả về `error_code`/`errno` = 4 (`存储好像出问题了`
    — "kho lưu trữ dường như đang gặp sự cố"). KHÁC với `error_code=2` (tham
    số/1 fs_id cụ thể bị hỏng, KHÔNG đáng retry nguyên batch): lỗi 4 là sự cố
    TẠM THỜI phía hạ tầng lưu trữ Baidu, ảnh hưởng cả batch nhưng thường tự
    hết sau vài giây — ĐÁNG để retry lại NGUYÊN batch (không cần tách lẻ
    fs_id như error_code=2)."""
    errno, _errmsg = _extract_errno_errmsg(exc)
    if errno is not None:
        try:
            if int(errno) == 4:
                return True
        except (TypeError, ValueError):
            pass
    msg = str(exc)
    if re.search(r"error_code[\"']?\s*[:=]\s*4\b", msg, re.IGNORECASE):
        return True
    if re.search(r"\berrno[\"']?\s*[:=]\s*4\b", msg, re.IGNORECASE):
        return True
    return "存储好像出问题了" in msg


def _transfer_fs_ids_individually(
    api: BaiduPCSApi,
    dest_dir: str,
    fs_ids: list[Any],
    uk: Any,
    share_id: Any,
    bdstoken: Any,
    share_url: str,
    batch_index: int,
    total_batches: int,
) -> tuple[list[Any], list[Any]]:
    """FALLBACK khi cả 1 batch bị Baidu từ chối với `error_code=2` (`参数错误`)
    — thường do 1 (vài) `fs_id` CỤ THỂ trong batch bị hỏng/đã bị xoá khỏi
    share/không còn hợp lệ, KHÔNG phải lỗi của toàn bộ batch. Thay vì để 1
    file hỏng chặn đứng cả batch, hàm này TÁCH batch ra, transfer TỪNG
    `fs_id` RIÊNG LẺ — `fs_id` nào tiếp tục dính `error_code=2` sẽ bị BỎ QUA
    (log WARNING, KHÔNG raise), các file `.mp4` còn lại trong bộ vẫn được
    transfer bình thường. Chỉ raise nếu TẤT CẢ `fs_id` lẻ trong batch đều
    thất bại (không transfer thành công được file nào).

    Trả về `(results, skipped_fs_ids)` — `skipped_fs_ids` để caller lọc
    CHÍNH XÁC những entry nào THẬT SỰ transfer thành công (dùng cho
    `saved_paths` trong callback, tránh báo về file chưa hề nằm trên Cloud)."""
    results: list[Any] = []
    skipped: list[Any] = []
    for fs_id in fs_ids:
        last_exc: Exception | None = None
        for attempt in range(1, BAIDU_TRANSFER_MAX_RETRIES + 1):
            try:
                result = api.transfer_shared_paths(
                    dest_dir, [fs_id], uk, share_id, bdstoken, share_url,
                )
                results.append(result)
                last_exc = None
                break
            except (BaiduPCSError, requests.RequestException, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001 - cần bắt mọi lỗi (kể cả BaiduPCSError) để quyết định skip/retry
                last_exc = exc
                if _is_error_code_4(exc):
                    # error_code=4 (存储好像出问题了) -> sự cố lưu trữ TẠM
                    # THỜI phía Baidu -> retry lại fs_id lẻ này, nghỉ RIÊNG
                    # BAIDU_ERROR_CODE_4_RETRY_SLEEP_SECONDS giây.
                    if attempt >= BAIDU_TRANSFER_MAX_RETRIES:
                        break
                    time.sleep(BAIDU_ERROR_CODE_4_RETRY_SLEEP_SECONDS)
                    continue
                if _is_error_code_2(exc):
                    # error_code=2 ở file LẺ -> gần như chắc chắn CHÍNH file
                    # này hỏng/đã bị xoá khỏi share -> KHÔNG phải lỗi tạm
                    # thời, retry vô ích -> bỏ qua ngay, không tốn lượt retry.
                    break
                if attempt >= BAIDU_TRANSFER_MAX_RETRIES or not _is_retryable_transfer_error(exc):
                    break
                time.sleep(BAIDU_TRANSFER_BATCH_SLEEP_SECONDS)

        if last_exc is not None:
            skipped.append(fs_id)
            logger.warning(
                "[Batch %d/%d] fs_id=%r transfer LỖI (error_code=2 hoặc lỗi "
                "không retry được) — BỎ QUA file này, tiếp tục transfer các "
                "file .mp4 còn lại trong bộ. Lỗi gốc: %s",
                batch_index, total_batches, fs_id, last_exc,
            )

    if not results:
        raise BaiduDownloadError(
            f"Batch {batch_index}/{total_batches}: TẤT CẢ {len(fs_ids)} fs_id "
            f"lẻ (đã tách ra để né error_code=2) đều transfer thất bại — có "
            f"thể toàn bộ file trong batch này đều hỏng/không hợp lệ."
        )

    if skipped:
        logger.warning(
            "[Batch %d/%d] Đã BỎ QUA %d/%d fs_id lỗi (error_code=2 hoặc lỗi "
            "không retry được), transfer THÀNH CÔNG %d/%d fs_id còn lại.",
            batch_index, total_batches, len(skipped), len(fs_ids),
            len(fs_ids) - len(skipped), len(fs_ids),
        )
    else:
        logger.info(
            "[Batch %d/%d] Transfer lẻ TẤT CẢ %d fs_id thành công (không có "
            "file nào bị bỏ qua).",
            batch_index, total_batches, len(fs_ids),
        )

    return results, skipped


def _dump_transfer_shared_paths_source(api: BaiduPCSApi) -> None:
    """CHẨN ĐOÁN: in ra SOURCE CODE THẬT của `transfer_shared_paths()` ở CẢ 2
    tầng (BaiduPCSApi.transfer_shared_paths và BaiduPCS.transfer_shared_paths
    bên dưới nó) — sau 3 lần đoán chữ ký sai theo 3 kiểu khác nhau (thiếu
    tham số -> sai thứ tự -> giá trị bị hoán vị trong query Baidu trả về
    "参数错误"), đoán tiếp qua traceback không còn hiệu quả. Hàm này chạy
    NGAY TRƯỚC khi gọi transfer thật, log ra đúng source + chữ ký của bản
    baidupcs-py ĐANG CÀI trên runner GitHub Actions — lần chạy tới sẽ biết
    chính xác 100% cần sửa gì, không đoán mò nữa.

    KHÔNG raise nếu lỗi (chỉ là chẩn đoán phụ trợ, không được chặn luồng
    chính nếu vì lý do gì đó không lấy được source, vd bản compiled/cython).
    """
    logger.info("=" * 70)
    logger.info("[DIAGNOSTIC] Dump source code thật của transfer_shared_paths()")
    try:
        sig_api = inspect.signature(api.transfer_shared_paths)
        logger.info("[DIAGNOSTIC] BaiduPCSApi.transfer_shared_paths%s", sig_api)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DIAGNOSTIC] Không lấy được signature ở tầng Api: %s", exc)

    try:
        src_api = inspect.getsource(api.transfer_shared_paths)
        logger.info("[DIAGNOSTIC] --- SOURCE tầng Api ---\n%s", src_api)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DIAGNOSTIC] Không lấy được source ở tầng Api: %s", exc)

    inner = getattr(api, "_baidupcs", None)
    inner_fn = getattr(inner, "transfer_shared_paths", None) if inner else None
    if callable(inner_fn):
        try:
            sig_inner = inspect.signature(inner_fn)
            logger.info(
                "[DIAGNOSTIC] BaiduPCS.transfer_shared_paths%s (tầng dưới)",
                sig_inner,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DIAGNOSTIC] Không lấy được signature tầng dưới: %s", exc)
        try:
            src_inner = inspect.getsource(inner_fn)
            logger.info("[DIAGNOSTIC] --- SOURCE tầng dưới (BaiduPCS) ---\n%s", src_inner)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DIAGNOSTIC] Không lấy được source tầng dưới: %s", exc)
    else:
        logger.warning("[DIAGNOSTIC] Không tìm thấy api._baidupcs.transfer_shared_paths.")

    try:
        import baidupcs_py

        logger.info("[DIAGNOSTIC] baidupcs-py version: %s", getattr(baidupcs_py, "__version__", "?"))
    except Exception:  # noqa: BLE001
        pass
    logger.info("=" * 70)


def _call_transfer_shared_paths(
    api: BaiduPCSApi,
    dest_dir: str,
    entries: list[Any],
    share_url: str,
    *,
    uk_override: Any = None,
    share_id_override: Any = None,
    bdstoken_override: Any = None,
) -> tuple[list[Any], set[Any]]:
    """Gọi `api.transfer_shared_paths()` với CHỮ KÝ THẬT — lần 3 xác nhận,
    lần này đối chiếu TRỰC TIẾP với `inspect.getsource()` dump tại runtime
    (xem `_dump_transfer_shared_paths_source`), tức đọc thẳng source code
    thật đang chạy trên máy CI chứ không suy đoán qua dòng forward nội bộ
    nữa. Dump đó cho ra CHỮ KÝ THẬT của tầng `BaiduPCSApi`:

        def transfer_shared_paths(self, remotedir, fs_ids, uk, share_id,
                                   bdstoken, shared_url): ...

    tức thứ tự đúng là remotedir, fs_ids, uk, share_id, bdstoken, shared_url
    — `fs_ids` đứng NGAY SAU `remotedir`, KHÔNG phải trước `shared_url` như
    lần sửa trước suy đoán nhầm (lần đó suy từ dòng forward nội bộ
    `self._baidupcs.transfer_shared_paths(remotedir, fs_ids, uk, share_id,
    bdstoken, shared_url)`, nhưng dòng forward đó chỉ nói tầng dưới nhận gì
    theo tên biến cục bộ — KHÔNG chứng minh được thứ tự tham số của CHÍNH
    hàm tầng `BaiduPCSApi`). Gọi sai thứ tự khiến giá trị `uk` (một int) bị
    truyền nhầm vào slot `fs_ids`, nên `dump_json(fs_ids)` serialize ra một
    số đơn lẻ thay vì list `[fs_id, ...]` -> Baidu trả lỗi
    `errno=2, message=参数错误` (tham số không hợp lệ). `fs_ids` vẫn là 1 LIST
    duy nhất — KHÔNG unpack bằng `*` (hàm không có `*fs_ids` biến đổi, chỉ
    có đúng 1 slot `fs_ids` nhận list).

    `uk`/`share_id`/`bdstoken` lấy từ field cùng tên có sẵn trên mỗi
    `PcsSharedPath` (kết quả `shared_paths()`) — giống nhau cho mọi entry
    trong cùng 1 link share, chỉ cần lấy từ entry đầu tiên. `shared_url`
    PHẢI là share_url GỐC (giữ `?pwd=...` nếu có, cùng giá trị đã dùng gọi
    `shared_paths()`).

    FIX (504 Gateway Timeout + JSONDecodeError khi fs_ids quá dài): thay vì
    gửi TOÀN BỘ fs_ids trong 1 request `transfer_shared_paths()` duy nhất
    (dễ khiến Baidu xử lý quá lâu -> 504 -> body trả về không phải JSON hợp
    lệ -> JSONDecodeError), hàm này CHIA NHỎ fs_ids thành từng batch tối đa
    `BAIDU_TRANSFER_BATCH_SIZE` fs_id, rồi gọi transfer TUẦN TỰ từng batch
    (nghỉ `BAIDU_TRANSFER_BATCH_SLEEP_SECONDS` giây giữa 2 batch để né rate
    limit). Mỗi batch tự retry tối đa `BAIDU_TRANSFER_MAX_RETRIES` lần nếu
    dính lỗi mạng/504/JSONDecodeError (`_is_retryable_transfer_error`) —
    lỗi không thuộc nhóm này (vd sai tham số, hết dung lượng, token hết
    hạn) sẽ raise NGAY, không retry vô ích.

    BATCH ISOLATION (soft-fail): nếu 1 batch retry hết
    `BAIDU_TRANSFER_MAX_RETRIES` lần vẫn lỗi (vd file bị Baidu khoá/lỗi lưu
    trữ không tự hồi phục), hàm này KHÔNG raise chết cả quá trình — log
    ERROR, BỎ QUA (skip) batch hỏng đó, rồi TIẾP TỤC transfer các batch còn
    lại. CHỈ raise `BaiduDownloadError` nếu TẤT CẢ batch đều thất bại (không
    có file nào transfer thành công) — chỉ cần ÍT NHẤT 1 batch thành công là
    coi như có kết quả để tiếp tục xử lý.

    FALLBACK RIÊNG cho `error_code=2` (`参数错误`, xem `_is_error_code_2`):
    lỗi này thường do 1 fs_id CỤ THỂ trong batch bị hỏng/đã bị xoá khỏi
    share, KHÔNG phải lỗi của toàn bộ batch -> KHÔNG raise chết cả batch mà
    TÁCH RA transfer từng fs_id lẻ (`_transfer_fs_ids_individually`), fs_id
    nào tiếp tục lỗi 2 thì log WARNING bỏ qua, các file .mp4 còn lại trong
    bộ vẫn transfer bình thường.

    RETRY RIÊNG cho `error_code=4` (`存储好像出问题了`, xem `_is_error_code_4`):
    sự cố lưu trữ TẠM THỜI phía hạ tầng Baidu (bao gồm cả `BaiduPCSError` do
    baidupcs-py raise ra) — retry lại NGUYÊN batch, nghỉ
    `BAIDU_ERROR_CODE_4_RETRY_SLEEP_SECONDS` giây giữa các lần thử, đủ
    `BAIDU_TRANSFER_MAX_RETRIES` lần trước khi coi batch đó là hỏng và
    chuyển qua cơ chế BATCH ISOLATION ở trên.

    Trả về `(results, skipped_fs_ids)` — `skipped_fs_ids` (set) chứa TẤT CẢ
    fs_id KHÔNG transfer thành công (do batch bị skip hoặc do fallback tách
    lẻ vẫn lỗi), để caller lọc CHÍNH XÁC `saved_paths` báo về callback, tránh
    báo nhầm file chưa hề thực sự nằm trên Cloud cá nhân.
    """
    if not entries:
        raise BaiduDownloadError("Không có entry nào để transfer (danh sách rỗng).")

    # FIX (404 Not Found -> JSONDecodeError): ĐẢM BẢO 100% thư mục đích
    # `dest_dir` (`/app_temp_download`) ĐÃ TỒN TẠI trên Baidu Cloud TRƯỚC KHI
    # bắt đầu vòng lặp transfer batch — nếu thư mục chưa có (lần chạy đầu
    # tiên, hoặc đã bị xoá bởi bước dọn dẹp trước đó mà chưa kịp tạo lại),
    # Baidu trả về 404 Not Found dưới dạng HTML thay vì JSON -> baidupcs-py
    # cố json.loads() body đó crash JSONDecodeError. Bọc try...except vì
    # makedir() có thể raise nếu thư mục ĐÃ tồn tại sẵn (không sao, bỏ qua).
    for method_name in ("makedir", "mkdir"):
        makedir_fn = getattr(api, method_name, None)
        if callable(makedir_fn):
            try:
                makedir_fn(dest_dir)
                logger.info(
                    "[BaiduPCS] Đã đảm bảo thư mục đích '%s' tồn tại trên Cloud "
                    "(gọi api.%s()) trước khi transfer.", dest_dir, method_name,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[BaiduPCS] api.%s('%s') lỗi (nhiều khả năng do thư mục ĐÃ "
                    "tồn tại sẵn — bỏ qua, không chặn transfer): %s",
                    method_name, dest_dir, exc,
                )
            break

    first = entries[0]
    # uk/share_id/bdstoken là thuộc tính CHUNG của CẢ link share (không đổi
    # theo từng entry con) — nếu entry (vd: file .mp4 đào được từ 1 Folder
    # con lồng sâu) không tự mang theo field này, fallback về giá trị đã lấy
    # từ danh sách entry CẤP GỐC (`shared_paths()`) do caller truyền vào qua
    # *_override, thay vì raise lỗi "thiếu field" oan uổng.
    uk = getattr(first, "uk", None) if uk_override is None else uk_override
    share_id = getattr(first, "share_id", None) if share_id_override is None else share_id_override
    bdstoken = getattr(first, "bdstoken", None) if bdstoken_override is None else bdstoken_override
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

    batches = [
        fs_ids[i : i + BAIDU_TRANSFER_BATCH_SIZE]
        for i in range(0, len(fs_ids), BAIDU_TRANSFER_BATCH_SIZE)
    ]
    logger.info(
        "Chia %d fs_id thành %d batch (tối đa %d fs_id/batch) để transfer "
        "tuần tự — né lỗi 504/JSONDecodeError khi gửi 1 lần quá to.",
        len(fs_ids), len(batches), BAIDU_TRANSFER_BATCH_SIZE,
    )

    # QUAN TRỌNG: thứ tự đúng (đối chiếu inspect.getsource() dump tại
    # runtime) là remotedir, fs_ids, uk, share_id, bdstoken, shared_url.
    # fs_ids của MỖI BATCH vẫn truyền NGUYÊN 1 LIST (không *unpack).
    results: list[Any] = []
    # BATCH ISOLATION: batch nào retry hết BAIDU_TRANSFER_MAX_RETRIES lần vẫn
    # lỗi (không phục hồi được, vd error_code=4 dai dẳng) sẽ bị BỎ QUA — gom
    # TOÀN BỘ fs_id KHÔNG transfer thành công vào đây (dù do skip nguyên batch
    # hay do fallback tách lẻ vẫn lỗi 1 phần), để caller lọc đúng saved_paths.
    skipped_fs_ids: set[Any] = set()
    for batch_index, batch_fs_ids in enumerate(batches, start=1):
        last_exc: Exception | None = None
        # Ngân sách retry RIÊNG cho error_code=4 (exponential backoff, độc
        # lập với BAIDU_TRANSFER_MAX_RETRIES) — track ở NGOÀI vòng attempt
        # chính để 1 batch có thể "chuyển làn" từ lỗi-4 sang lỗi-khác (hoặc
        # ngược lại) mà không bị tính trùng/tính thiếu lượt thử.
        error_code_4_attempt = 0
        attempt = 0
        while attempt < BAIDU_TRANSFER_MAX_RETRIES:
            attempt += 1
            try:
                logger.debug(
                    "Batch %d/%d (lần thử %d/%d) — transfer_shared_paths(remotedir=%r, "
                    "fs_ids=%r, uk=%r, share_id=%r, bdstoken=%r..., shared_url=%r)",
                    batch_index, len(batches), attempt, BAIDU_TRANSFER_MAX_RETRIES,
                    dest_dir, batch_fs_ids, uk, share_id,
                    (bdstoken[:8] + "...") if isinstance(bdstoken, str) else bdstoken,
                    share_url,
                )
                result = api.transfer_shared_paths(
                    dest_dir, batch_fs_ids, uk, share_id, bdstoken, share_url,
                )
                results.append(result)
                logger.info(
                    "Batch %d/%d (%d fs_id) transfer THÀNH CÔNG (lần thử %d/%d).",
                    batch_index, len(batches), len(batch_fs_ids), attempt,
                    BAIDU_TRANSFER_MAX_RETRIES,
                )
                last_exc = None
                break
            # Bắt rõ cả BaiduPCSError (lỗi API gốc từ baidupcs-py, vd
            # error_code=4 "存储好像出问题了") thay vì chỉ trông chờ except
            # Exception chung chung — để nhánh retry-thật-sự cho error_code=4
            # luôn được xét TRƯỚC, không lọt xuống nhánh "không retry được".
            except (BaiduPCSError, requests.RequestException, json.JSONDecodeError, Exception) as exc:  # noqa: BLE001 - cần bắt mọi lỗi để quyết định retry
                last_exc = exc
                if _is_error_code_4(exc):
                    # FIX: error_code=4 (存储好像出问题了) giờ dùng ngân sách
                    # retry RIÊNG (BAIDU_ERROR_CODE_4_MAX_RETRIES lần, backoff
                    # tăng dần 5s/10s/20s/40s/60s/60s) thay vì ăn chung 3 lần
                    # với lỗi mạng — log thực tế cho thấy 2s x 3 lần LÀ QUÁ
                    # NGẮN để sự cố lưu trữ phía Baidu tự hồi, khiến batch
                    # (và cả link) chết oan dù lỗi này vốn tạm thời.
                    error_code_4_attempt += 1
                    if error_code_4_attempt >= BAIDU_ERROR_CODE_4_MAX_RETRIES:
                        logger.warning(
                            "Batch %d/%d dính error_code=4 (存储好像出问题了) "
                            "đã retry đủ %d/%d lần (ngân sách riêng) vẫn lỗi "
                            "— sẽ ghi nhận batch này THẤT BẠI (BỎ QUA): %s",
                            batch_index, len(batches), error_code_4_attempt,
                            BAIDU_ERROR_CODE_4_MAX_RETRIES, exc,
                        )
                        break
                    sleep_sec = min(
                        BAIDU_ERROR_CODE_4_BASE_SLEEP_SECONDS * (2 ** (error_code_4_attempt - 1)),
                        BAIDU_ERROR_CODE_4_MAX_SLEEP_SECONDS,
                    )
                    logger.warning(
                        "Batch %d/%d dính error_code=4 (存储好像出问题了 — lỗi "
                        "lưu trữ tạm thời phía Baidu, lần thử %d/%d riêng cho "
                        "lỗi này) — nghỉ %.1fs rồi retry nguyên batch: %s",
                        batch_index, len(batches), error_code_4_attempt,
                        BAIDU_ERROR_CODE_4_MAX_RETRIES, sleep_sec, exc,
                    )
                    time.sleep(sleep_sec)
                    # KHÔNG tính vào ngân sách `attempt` của vòng lặp chính
                    # (chỉ dành cho lỗi mạng/504/JSONDecodeError) -> lùi lại,
                    # để error_code=4 có ngân sách retry hoàn toàn riêng.
                    attempt -= 1
                    continue
                if _is_error_code_2(exc):
                    # error_code=2 (参数错误) -> thường do 1 fs_id CỤ THỂ
                    # trong batch bị hỏng/không hợp lệ, KHÔNG phải lỗi mạng
                    # tạm thời -> retry lại NGUYÊN batch vô ích, dừng ngay để
                    # rơi xuống fallback tách lẻ fs_id bên dưới.
                    logger.warning(
                        "Batch %d/%d dính error_code=2 (参数错误, lần thử %d/%d) "
                        "— dừng retry nguyên batch, sẽ TÁCH LẺ %d fs_id để "
                        "xác định + bỏ qua đúng file hỏng: %s",
                        batch_index, len(batches), attempt, BAIDU_TRANSFER_MAX_RETRIES,
                        len(batch_fs_ids), exc,
                    )
                    break
                retryable = _is_retryable_transfer_error(exc)
                if attempt >= BAIDU_TRANSFER_MAX_RETRIES or not retryable:
                    logger.warning(
                        "Batch %d/%d lỗi (lần thử %d/%d, %s): %s",
                        batch_index, len(batches), attempt, BAIDU_TRANSFER_MAX_RETRIES,
                        "KHÔNG retry được nữa" if not retryable else "đã hết lượt retry",
                        exc,
                    )
                    break
                logger.warning(
                    "Batch %d/%d lỗi mạng/504/JSONDecodeError (lần thử %d/%d) — "
                    "sẽ retry sau %.1fs: %s",
                    batch_index, len(batches), attempt, BAIDU_TRANSFER_MAX_RETRIES,
                    BAIDU_TRANSFER_BATCH_SLEEP_SECONDS, exc,
                )
                time.sleep(BAIDU_TRANSFER_BATCH_SLEEP_SECONDS)

        if last_exc is not None:
            if _is_error_code_2(last_exc) and len(batch_fs_ids) > 1:
                # FALLBACK: batch lỗi error_code=2 -> tách lẻ, transfer từng
                # fs_id — file nào tiếp tục lỗi 2 thì bỏ qua (log WARNING),
                # KHÔNG chặn cả batch/cả bộ phim vì 1 file hỏng.
                try:
                    batch_result, ind_skipped = _transfer_fs_ids_individually(
                        api, dest_dir, batch_fs_ids, uk, share_id, bdstoken, share_url,
                        batch_index, len(batches),
                    )
                    results.append(batch_result)
                    skipped_fs_ids.update(ind_skipped)
                except BaiduDownloadError as fallback_exc:
                    # TẤT CẢ fs_id lẻ trong batch đều thất bại -> BATCH
                    # ISOLATION: log ERROR + BỎ QUA nguyên batch này, KHÔNG
                    # raise chết cả workflow, tiếp tục các batch còn lại.
                    skipped_fs_ids.update(batch_fs_ids)
                    logger.error(
                        "Batch %d/%d: fallback tách lẻ fs_id CŨNG thất bại "
                        "hoàn toàn — BỎ QUA (skip) toàn bộ batch này, TIẾP "
                        "TỤC transfer các batch còn lại: %s",
                        batch_index, len(batches), fallback_exc,
                    )
            else:
                # BATCH ISOLATION (soft-fail): batch đã retry hết
                # BAIDU_TRANSFER_MAX_RETRIES lần (bao gồm cả nhánh
                # error_code=4 ở trên) vẫn lỗi -> log ERROR + BỎ QUA batch
                # hỏng này, KHÔNG raise làm crash toàn bộ workflow — tiếp tục
                # các batch kế tiếp, fs_id của batch này đơn giản sẽ thiếu
                # trong `saved_paths` cuối cùng thay vì làm mất TẤT CẢ.
                skipped_fs_ids.update(batch_fs_ids)
                logger.error(
                    "Batch %d/%d (%d fs_id) transfer THẤT BẠI sau %d lần thử "
                    "— BỎ QUA (skip) batch lỗi này (có thể do file bị Baidu "
                    "khoá/lỗi lưu trữ không tự hồi phục), TIẾP TỤC transfer "
                    "các batch còn lại: %s",
                    batch_index, len(batches), len(batch_fs_ids),
                    BAIDU_TRANSFER_MAX_RETRIES, last_exc,
                )

        # Nghỉ giữa các batch (kể cả sau batch cuối thì không cần) để né rate
        # limit — Baidu dễ trả 504 nếu request dồn dập liên tục.
        if batch_index < len(batches):
            time.sleep(BAIDU_TRANSFER_BATCH_SLEEP_SECONDS)

    if skipped_fs_ids:
        logger.warning(
            "Hoàn tất transfer: %d/%d fs_id THÀNH CÔNG, %d fs_id BỊ BỎ QUA "
            "(lỗi không phục hồi được sau %d lần thử/batch) vào %s.",
            len(fs_ids) - len(skipped_fs_ids), len(fs_ids), len(skipped_fs_ids),
            BAIDU_TRANSFER_MAX_RETRIES, dest_dir,
        )
    else:
        logger.info(
            "TẤT CẢ %d batch (%d fs_id) đã transfer thành công vào %s.",
            len(batches), len(fs_ids), dest_dir,
        )

    if not results:
        # KHÔNG batch nào transfer thành công -> thật sự không có gì để tiếp
        # tục -> raise để tầng gọi biết job thất bại hoàn toàn (khác với "1
        # vài batch lỗi nhưng vẫn còn kết quả" ở trên, vốn KHÔNG raise nữa).
        raise BaiduDownloadError(
            f"TẤT CẢ {len(batches)} batch ({len(fs_ids)} fs_id) đều transfer "
            f"thất bại vào {dest_dir} — không có file nào được transfer "
            f"thành công (xem log ERROR phía trên để biết lý do từng batch)."
        )

    return results, skipped_fs_ids


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


def _list_personal_cloud_mp4s(
    bduss: str, stoken: "str | None", bdstoken: Any, dest_dir: str,
) -> list[str]:
    """FIX VÒNG 2 (log thực tế 14/08/2026 — job chạy tiếp sau fix quét lại
    Cloud cá nhân): bản đầu dùng `api.list(dest_dir, recursive=True)` —
    NHƯNG method này của baidupcs-py 0.7.6 CŨNG gọi thẳng domain PCS CŨ
    `pcs.baidu.com` (app_id=778750 hardcode cứng), domain đã bị Baidu khai
    tử/chặn HOÀN TOÀN (y hệt lỗi đã fix ở `_resolve_download_links_via_rest_api`
    cho `api.meta()`/`api.download_link()` — xem docstring hàm đó) -> 400
    `error_code: 31023, message: 输入参数错误` cho MỌI lệnh gọi, kể cả `mkdir`.

    Sửa: BỎ HẲN `api.list()`, tự gọi THẲNG REST API hiện đại
    `pan.baidu.com/api/list` bằng `requests` — ĐÚNG domain (`pan.baidu.com`,
    KHÔNG PHẢI `pcs.baidu.com`) + ĐÚNG `app_id=250528` đã CHỨNG MINH hoạt
    động thật qua `transfer_shared_paths()` VÀ qua REST `/api/filemetas`
    (cùng kiểu xác thực cookie BDUSS/STOKEN + bdstoken, không cần OAuth
    access_token như domain PCS cũ). Đệ quy thủ công vào từng thư mục con
    (không dùng `recursive=1` của endpoint này vì hành vi/giới hạn không rõ
    ràng bằng tự đệ quy theo `isdir`)."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://pan.baidu.com/disk/home",
        "X-Requested-With": "XMLHttpRequest",
    })
    session.cookies.set("BDUSS", bduss, domain=".baidu.com")
    if stoken:
        session.cookies.set("STOKEN", stoken, domain=".baidu.com")

    mp4_paths: list[str] = []

    def _list_dir(remote_dir: str, _depth: int = 0, _max_depth: int = 12) -> None:
        if _depth > _max_depth:
            logger.warning(
                "[REST] Đã đệ quy quá %d cấp tại %s — dừng để tránh vòng lặp.",
                _max_depth, remote_dir,
            )
            return
        start = 0
        limit = 1000
        while True:
            params = {
                "dir": remote_dir,
                "order": "name",
                "desc": "0",
                "start": str(start),
                "limit": str(limit),
                "web": "1",
                "channel": "chunlei",
                "clienttype": "0",
                "app_id": "250528",
            }
            if bdstoken:
                params["bdstoken"] = bdstoken
            try:
                resp = session.get("https://pan.baidu.com/api/list", params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise BaiduDownloadError(
                    f"[REST] GET /api/list (dir={remote_dir!r}, start={start}) lỗi: {exc}"
                ) from exc

            errno = data.get("errno")
            if errno not in (0, None):
                raise BaiduDownloadError(
                    f"[REST] /api/list (dir={remote_dir!r}) trả errno={errno} (raw={data!r})"
                )

            items = data.get("list") or []
            for entry in items:
                path = entry.get("path")
                if not path:
                    continue
                if entry.get("isdir"):
                    _list_dir(path, _depth=_depth + 1, _max_depth=_max_depth)
                elif Path(path).suffix.lower() == ".mp4":
                    mp4_paths.append(path)

            if len(items) < limit:
                break
            start += limit

    logger.info("[REST] Quét lại Cloud cá nhân %s (SAU transfer, gọi TRỰC TIẾP pan.baidu.com/api/list) để lấy path thật...", dest_dir)
    _list_dir(dest_dir)
    logger.info(
        "[REST] Quét lại %s -> tìm thấy %d file .mp4 THẬT trên Cloud cá nhân.",
        dest_dir, len(mp4_paths),
    )
    return mp4_paths


def _dump_download_link_diagnostics(api: "BaiduPCSApi") -> None:
    """CHẨN ĐOÁN (theo đúng pattern đã dùng ở `_dump_transfer_shared_paths_source`
    để fix `transfer_shared_paths` trước đây): log thực tế 13/08/2026 cho
    thấy CẢ 3 tên đoán (`download_link`/`download_url`/`dlink`) ĐỀU KHÔNG
    TỒN TẠI trên bản baidupcs-py đang cài trên runner (0/200 file resolve
    được, không có exception nào bị log ra — nghĩa là `getattr(...)` trả
    `None` ngay từ đầu, method chưa từng thật sự được gọi). Đoán tiếp không
    hiệu quả -> dump ra MỌI method có tên liên quan download/link/meta/url
    kèm signature thật, chạy 1 LẦN DUY NHẤT trước khi thử resolve, để nếu
    vẫn lỗi thì log lần này đã đủ thông tin sửa dứt điểm, không cần thêm
    vòng đoán mò nào nữa."""
    logger.info("=" * 70)
    logger.info("[DIAGNOSTIC] Liệt kê method liên quan download/link/meta/url trên api:")
    keywords = ("download", "link", "dlink", "meta", "url")
    found_any = False
    for name in sorted(dir(api)):
        if name.startswith("_"):
            continue
        lname = name.lower()
        if not any(k in lname for k in keywords):
            continue
        attr = getattr(api, name, None)
        if not callable(attr):
            continue
        found_any = True
        try:
            sig = inspect.signature(attr)
            logger.info("[DIAGNOSTIC]   api.%s%s", name, sig)
        except (TypeError, ValueError) as exc:
            logger.info("[DIAGNOSTIC]   api.%s(...) (không lấy được signature: %s)", name, exc)
    if not found_any:
        logger.warning(
            "[DIAGNOSTIC] KHÔNG tìm thấy method nào khớp từ khoá download/"
            "link/meta/url trên api — rất lạ, kiểm tra lại object api."
        )
    try:
        import baidupcs_py
        logger.info("[DIAGNOSTIC] baidupcs-py version: %s", getattr(baidupcs_py, "__version__", "?"))
        logger.info("[DIAGNOSTIC] baidupcs_py module path: %s", getattr(baidupcs_py, "__file__", "?"))
    except Exception:  # noqa: BLE001
        pass
    logger.info("=" * 70)


def _extract_dlink_from_entry(entry: Any) -> "str | None":
    """1 `PcsFile` trả về từ `api.meta(*remotepaths)` có thể lộ dlink qua
    vài tên field khác nhau tuỳ bản baidupcs-py (object attribute hoặc dict
    key) -> thử cả 2 kiểu, với vài tên field hay gặp."""
    for field in ("dlink", "download_url", "downloadurl", "url"):
        val = getattr(entry, field, None) if not isinstance(entry, dict) else entry.get(field)
        if val:
            return val
    return None


def _resolve_download_links_batch(api: "BaiduPCSApi", remote_paths: list[str]) -> "dict[str, str] | None":
    """Thử phương án BATCH trước tiên qua `api.meta(*remotepaths)` — method
    THẬT SỰ TỒN TẠI trên bản đang cài (xác nhận qua diagnostic dump 13/08/2026:
    `api.meta(*remotepaths: str) -> List[PcsFile]`). Lưu ý chữ ký dùng
    *args (variadic), KHÔNG nhận list -> phải gọi `fn(*remote_paths)`, không
    phải `fn(remote_paths)`.

    Đây chỉ là "may rủi": PcsFile trả về CHƯA CHẮC đã có sẵn field dlink (rất
    có thể chỉ là metadata thường, dlink phải xin riêng qua
    `download_link()`) — nếu không entry nào có dlink, trả `None` để tầng
    gọi rơi xuống fallback per-file bằng `download_link()` (method CHẮC CHẮN
    đúng theo diagnostic dump)."""
    fn = getattr(api, "meta", None)
    if not callable(fn):
        return None
    try:
        results = fn(*remote_paths)
    except Exception as exc:  # noqa: BLE001
        logger.warning("api.meta(*remotepaths) lỗi: %s", exc)
        return None

    if not results:
        logger.warning("api.meta(...) trả về rỗng.")
        return None

    download_urls: dict[str, str] = {}
    for entry in results:
        path = getattr(entry, "path", None) or (entry.get("path") if isinstance(entry, dict) else None)
        if not path:
            continue
        url = _extract_dlink_from_entry(entry)
        if url:
            download_urls[path] = url
    logger.info(
        "[BATCH] api.meta(*remotepaths) trả %d entry -> resolve được %d/%d dlink "
        "(PcsFile có thể không mang sẵn dlink -> %d còn lại sẽ resolve per-file).",
        len(results), len(download_urls), len(remote_paths), len(remote_paths) - len(download_urls),
    )
    return download_urls or None


def _resolve_single_download_link(api: "BaiduPCSApi", remote_path: str) -> "str | None":
    """FIX VÒNG 4 (log thực tế 14/08/2026): vòng trước thử CẢ `pcs=False` VÀ
    `pcs=True` — kết quả 200/200 "resolve thành công" nhưng TOÀN BỘ URL cuối
    cùng dùng đều là dạng `c.pcs.baidu.com/rest/2.0/pcs/file?method=download
    &app_id=778750...` (dạng PCS OpenAPI CŨ) -> tải 404 100%. Endpoint PCS cũ
    này cần xác thực bằng `access_token` OAuth2 CHỨ KHÔNG PHẢI cookie
    BDUSS/STOKEN mà toàn bộ hệ thống đang dùng để đăng nhập -> `pcs=True`
    KHÔNG BAO GIỜ hoạt động được với kiểu đăng nhập hiện tại, dù trả về
    "url" (url đó vô dụng, luôn 404 khi tải).

    Bỏ hẳn nhánh `pcs=True` — CHỈ dùng `pcs=False` (luồng share API mới,
    tương thích cookie BDUSS). Nếu `pcs=False` trả `None`/rỗng (không
    raise), log RÕ RÀNG ngay tại đây (khác bản trước — bản trước im lặng
    nếu không có exception, khiến không biết pcs=False có thật sự được gọi
    hay không) để log GH Actions lần chạy tới cho biết CHÍNH XÁC nguyên
    nhân — không đoán mò vòng 5."""
    fn = getattr(api, "download_link", None)
    if not callable(fn):
        logger.warning("api.download_link không tồn tại trên object api.")
        return None
    try:
        url = fn(remote_path, pcs=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "api.download_link('%s', pcs=False) RAISE exception: %s",
            remote_path, exc,
        )
        return None
    if url:
        return url
    logger.warning(
        "api.download_link('%s', pcs=False) trả về giá trị RỖNG/None "
        "(KHÔNG raise exception) — đây là toàn bộ thông tin có được, "
        "baidupcs-py không cho biết lý do cụ thể hơn ở tầng này.",
        remote_path,
    )
    return None


def _resolve_download_links_via_rest_api(
    bduss: str, stoken: "str | None", bdstoken: Any, remote_paths: list[str],
) -> dict[str, str]:
    """
    FIX VÒNG 5 (log 14/08/2026 — XÁC NHẬN CHẮC CHẮN qua log GH Actions):
    `api.meta()` gọi thẳng `pcs.baidu.com/rest/2.0/pcs/file?method=meta`
    (domain PCS CŨ, app_id=778750 hardcode cứng trong baidupcs-py 0.7.6) và
    ăn HTTP 404 — domain `pcs.baidu.com` này đã bị Baidu khai tử/chặn HOÀN
    TOÀN cho app_id mặc định của thư viện, không phân biệt endpoint
    (meta/download đều 404 như nhau). `download_link(pcs=False)` nhiều khả
    năng bên trong CŨNG phụ thuộc domain PCS cũ này để lấy metadata trước
    khi build link -> luôn âm thầm trả `None` (baidupcs-py tự bắt lỗi,
    không raise ra ngoài cho ta thấy).

    Giải pháp: BỎ HẲN wrapper `api.meta()`/`api.download_link()` của
    baidupcs-py cho bước resolve dlink — tự gọi THẲNG REST API hiện đại
    `pan.baidu.com/api/filemetas?dlink=1` bằng `requests`, dùng ĐÚNG domain
    (`pan.baidu.com`, KHÔNG PHẢI `pcs.baidu.com`) + ĐÚNG `app_id=250528` đã
    CHỨNG MINH hoạt động thật qua `transfer_shared_paths()` (xem
    `_dump_transfer_shared_paths_source` — cùng domain, cùng app_id, cùng
    kiểu xác thực cookie BDUSS/STOKEN + bdstoken, không cần OAuth
    access_token như domain PCS cũ).

    FIX VÒNG 6 (log thực tế 14/08/2026 — XÁC NHẬN qua log GH Actions lần
    chạy sau vòng 5): batch cố định 100 path/request (vòng 5) khiến URL GET
    dài TỚI HÀNG CHỤC NGHÌN KÝ TỰ khi path chứa nhiều ký tự CJK (mỗi ký tự
    Hán/Trung sau url-encode nặng gấp ~9 lần, vd '妈' -> '%E5%A6%88') ->
    Baidu trả `414 Request-URI Too Large` cho CẢ 2 batch (0/200 dlink resolve
    được) -> rơi hẳn xuống fallback per-file (đã CHỨNG MINH luôn thất bại ở
    vòng 5, xem docstring `_resolve_download_links`) -> BaiduDownloadError.

    Sửa: CHIA BATCH THEO ĐỘ DÀI URL SAU KHI ENCODE (không còn theo SỐ LƯỢNG
    path cố định) — cộng dồn độ dài `urllib.parse.quote()` của từng path,
    cắt batch mới khi tổng vượt `_REST_FILEMETAS_MAX_QUERY_CHARS` (ngưỡng AN
    TOÀN, thấp hơn nhiều so với giới hạn thực tế của server để chừa margin
    cho các tham số khác + an toàn với server/proxy có ngưỡng thấp hơn
    Chrome/Firefox mặc định ~8KB). Đề phòng ước lượng vẫn sai (server có
    ngưỡng thấp hơn dự kiến), nếu vẫn dính 414 thì TỰ ĐỘNG CHẺ ĐÔI batch đó
    và thử lại đệ quy tới khi mỗi batch chỉ còn 1 path (nếu 1 path vẫn 414,
    bỏ qua path đó — path đơn lẻ 414 gần như không thể, nhưng vẫn không để
    treo vô hạn)."""
    if not remote_paths:
        return {}

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://pan.baidu.com/disk/home",
        "X-Requested-With": "XMLHttpRequest",
    })
    session.cookies.set("BDUSS", bduss, domain=".baidu.com")
    if stoken:
        session.cookies.set("STOKEN", stoken, domain=".baidu.com")

    download_urls: dict[str, str] = {}

    def _fetch_batch(batch: list[str], label: str) -> None:
        """Gọi 1 batch — nếu dính 414 (URL vẫn quá dài dù đã ước lượng),
        CHẺ ĐÔI và thử lại đệ quy thay vì bỏ cuộc luôn cả batch."""
        params = {
            "target": json.dumps(batch, ensure_ascii=False),
            "dlink": "1",
            "web": "1",
            "channel": "chunlei",
            "clienttype": "0",
            "app_id": "250528",
        }
        if bdstoken:
            params["bdstoken"] = bdstoken
        try:
            resp = session.get(
                "https://pan.baidu.com/api/filemetas", params=params, timeout=30,
            )
            if resp.status_code == 414 and len(batch) > 1:
                mid = len(batch) // 2
                logger.warning(
                    "[REST] GET /api/filemetas (batch %s, %d path) vẫn dính "
                    "414 dù đã ước lượng độ dài trước -> CHẺ ĐÔI thành 2 "
                    "batch %d + %d path và thử lại.",
                    label, len(batch), mid, len(batch) - mid,
                )
                _fetch_batch(batch[:mid], f"{label}a")
                _fetch_batch(batch[mid:], f"{label}b")
                return
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[REST] GET /api/filemetas (batch %s, %d path) lỗi request: %s",
                label, len(batch), exc,
            )
            return

        errno = data.get("errno")
        if errno not in (0, None):
            logger.warning(
                "[REST] /api/filemetas (batch %s, %d path) trả errno=%s (raw=%r)",
                label, len(batch), errno, data,
            )
            return

        for entry in data.get("info") or []:
            path = entry.get("path")
            dlink = entry.get("dlink")
            if path and dlink:
                download_urls[path] = dlink
            elif path:
                logger.warning(
                    "[REST] /api/filemetas trả entry cho '%s' nhưng KHÔNG có "
                    "field 'dlink' (raw entry=%r)", path, entry,
                )

    # Ngưỡng AN TOÀN cho tổng độ dài các path ĐÃ URL-ENCODE trong 1 batch —
    # để margin rộng cho phần params khác (dlink/web/channel/app_id/bdstoken/
    # JSON list brackets+quotes+dấu phẩy) + an toàn với server/proxy có
    # ngưỡng thấp hơn 8KB mặc định của trình duyệt.
    _REST_FILEMETAS_MAX_QUERY_CHARS = 2000

    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_len = 0
    for path in remote_paths:
        encoded_len = len(quote(path, safe="")) + 3  # +3 cho dấu ngoặc kép + dấu phẩy trong JSON
        if current_batch and current_len + encoded_len > _REST_FILEMETAS_MAX_QUERY_CHARS:
            batches.append(current_batch)
            current_batch = []
            current_len = 0
        current_batch.append(path)
        current_len += encoded_len
    if current_batch:
        batches.append(current_batch)

    logger.info(
        "[REST] Chia %d path thành %d batch theo ĐỘ DÀI URL sau encode (ngưỡng "
        "%d ký tự/batch — path chứa nhiều CJK nên KHÔNG chia đều theo số "
        "lượng cố định như trước, để né lỗi 414 Request-URI Too Large).",
        len(remote_paths), len(batches), _REST_FILEMETAS_MAX_QUERY_CHARS,
    )

    for idx, batch in enumerate(batches, start=1):
        _fetch_batch(batch, f"{idx}/{len(batches)}")

    logger.info(
        "[REST] GET /api/filemetas (gọi TRỰC TIẾP pan.baidu.com, KHÔNG qua "
        "wrapper baidupcs-py) resolve được %d/%d dlink.",
        len(download_urls), len(remote_paths),
    )
    return download_urls


def _resolve_download_links(
    api: "BaiduPCSApi", remote_paths: list[str],
    bduss: str, stoken: "str | None", bdstoken: Any,
) -> dict[str, str]:
    """
    FIX (log thực tế 13/08/2026): HF Space gọi `api.download_link()` LOCAL
    để lấy direct-link CDN cho từng file sau khi GH Actions đã transfer xong
    -> 200/200 file đều lỗi "Không lấy được direct download link" — vì lấy
    dlink là 1 API call TỚI pan.baidu.com, chạy bằng IP HF Space (chính IP
    đã bị Baidu chặn, lý do phải proxy TOÀN BỘ thao tác Baidu qua GitHub
    Actions ngay từ đầu — không riêng gì bước transfer/list).

    Hàm này lấy dlink NGAY TRONG process GitHub Actions (IP không bị chặn),
    trả về dict {remotepath: dlink_url} qua webhook. HF Space CHỈ còn việc
    tải bytes bằng `requests` thẳng vào URL CDN đã ký sẵn này (kèm cookie
    BDUSS/STOKEN) — bản thân URL CDN không bị chặn theo IP (khác domain +
    khác cơ chế chặn so với API pan.baidu.com), chỉ cần đúng cookie xác
    thực để CDN chấp nhận request.

    FIX VÒNG 5 (QUAN TRỌNG NHẤT — log 14/08/2026 xác nhận qua GH Actions log):
    `api.meta()`/`api.download_link()` của baidupcs-py 0.7.6 ĐỀU phụ thuộc
    domain PCS cũ `pcs.baidu.com` (app_id=778750 hardcode) — domain này đã
    bị Baidu khai tử/chặn HOÀN TOÀN, luôn 404 bất kể endpoint gì. Ưu tiên
    dùng THẲNG `_resolve_download_links_via_rest_api()` (gọi trực tiếp
    `pan.baidu.com/api/filemetas?dlink=1`, đúng domain/app_id đã chứng minh
    hoạt động qua `transfer_shared_paths`). Chỉ khi cách này thất bại/thiếu
    file mới rơi xuống các cách cũ qua baidupcs-py (batch `meta()` + per-file
    `download_link()`) như lưới an toàn cuối, dù xác suất thành công thấp.
    """
    _dump_download_link_diagnostics(api)

    download_urls = _resolve_download_links_via_rest_api(bduss, stoken, bdstoken, remote_paths)
    missing = [p for p in remote_paths if p not in download_urls]
    if not missing:
        return download_urls

    logger.warning(
        "[REST] Còn %d/%d file chưa resolve được qua REST API trực tiếp — "
        "thử fallback qua wrapper baidupcs-py (xác suất thành công thấp, "
        "xem docstring `_resolve_download_links`).",
        len(missing), len(remote_paths),
    )

    batch_result = _resolve_download_links_batch(api, missing)
    if batch_result:
        download_urls.update(batch_result)
    missing = [p for p in missing if p not in download_urls]

    for remote_path in missing:
        url = _resolve_single_download_link(api, remote_path)
        if url:
            download_urls[remote_path] = url
        else:
            logger.warning(
                "KHÔNG resolve được dlink cho '%s' (đã thử REST API trực "
                "tiếp + api.meta() + api.download_link(pcs=False)) — file "
                "này sẽ bị BỎ QUA ở HF Space.",
                remote_path,
            )
    logger.info(
        "Đã resolve dlink cho %d/%d file .mp4 (trong process GH Actions, "
        "IP không bị chặn).", len(download_urls), len(remote_paths),
    )
    return download_urls


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

    raw_processed_ids = _env("PROCESSED_DRAMA_IDS", required=False)
    try:
        processed_drama_ids: set[str] = set(json.loads(raw_processed_ids)) if raw_processed_ids else set()
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "PROCESSED_DRAMA_IDS không parse được thành mảng JSON hợp lệ "
            "(raw=%r) — coi như rỗng, có thể dedup sai/random trùng bộ phim "
            "đã xử lý: %s", raw_processed_ids, exc,
        )
        processed_drama_ids = set()

    logger.info(
        "Job %s — resolving share_url=%s -> dest_dir=%s (%d drama_id đã xử lý trước đó)",
        job_id, share_url, dest_dir, len(processed_drama_ids),
    )

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
        # BƯỚC 2.5 — Random chọn 1 Folder bộ phim CHƯA có trong
        # processed_drama_ids (né trùng), rồi ĐÀO SÂU vào trong Folder đó để
        # lấy danh sách item con và LỌC CHỈ file `.mp4` — nếu link share
        # KHÔNG có Folder con (toàn file lẻ ở cấp gốc), coi cả link share là
        # 1 "bộ phim phẳng" và lọc .mp4 ngay trên danh sách cấp gốc.
        # ------------------------------------------------------------- #
        entries = list(shared_paths)
        dir_entries = [e for e in entries if _entry_is_dir(e)]
        root_uk = getattr(entries[0], "uk", None)
        root_share_id = getattr(entries[0], "share_id", None)
        root_bdstoken = getattr(entries[0], "bdstoken", None)

        if dir_entries:
            chosen_entry, drama_id = _pick_unprocessed_entry(dir_entries, processed_drama_ids)
            if chosen_entry is None:
                logger.info(
                    "Toàn bộ %d Folder bộ phim trong link share này đã có trong "
                    "processed_drama_ids — không còn gì mới để chọn.", len(dir_entries),
                )
                _send_callback(
                    callback_url, webhook_secret, job_id, {"status": "all_processed"},
                )
                return

            logger.info(
                "Đã chọn drama_id='%s' — đang đào sâu vào Folder '%s' để lọc "
                "file .mp4 (KHÔNG transfer nguyên fs_id thư mục mẹ)...",
                drama_id, _entry_path(chosen_entry),
            )
            mp4_entries = _collect_mp4_entries(
                api, chosen_entry, root_uk, root_share_id, root_bdstoken, share_url,
                target_folder_name=drama_id,
            )
            mp4_entries = _enforce_mp4_hard_cap(mp4_entries, drama_id)
        else:
            drama_id = _derive_flat_drama_id(share_url)
            if drama_id in processed_drama_ids:
                logger.info(
                    "Link share này (không có Folder con, drama_id='%s') đã được "
                    "xử lý trước đó.", drama_id,
                )
                _send_callback(
                    callback_url, webhook_secret, job_id, {"status": "all_processed"},
                )
                return
            mp4_entries = [
                e for e in entries
                if not _entry_is_dir(e) and (_entry_path(e) or "").lower().endswith(".mp4")
            ]
            mp4_entries = _enforce_mp4_hard_cap(mp4_entries, drama_id)

        if not mp4_entries:
            raise BaiduDownloadError(
                f"Không tìm thấy file .mp4 nào (đã lọc is_file + đuôi .mp4, kể "
                f"cả Folder con lồng nhau) cho drama_id='{drama_id}' — có thể "
                f"Folder này chỉ chứa phụ đề/ảnh/rác, hoặc share đã die."
            )

        # BƯỚC 2.6 — Natural sort theo số tập trích từ TÊN FILE bằng Regex,
        # để `saved_paths` trả về (và fs_ids truyền vào transfer) ĐÃ ĐÚNG thứ
        # tự tập tăng dần — HF Space tải về theo đúng thứ tự này, không cần
        # tự sort lại (dù `automation.py` vẫn tự sort lại 1 lần nữa cho an
        # toàn kép, xem `_natural_sort_episodes`).
        mp4_entries.sort(key=_natural_sort_key)
        logger.info(
            "Đã lọc + sort được %d file .mp4 cho drama_id='%s': %s",
            len(mp4_entries), drama_id,
            [Path(_entry_path(e) or "").name for e in mp4_entries],
        )

        # ------------------------------------------------------------- #
        # BƯỚC 3 — transfer_shared_paths() PHẢI chạy cùng process với
        # shared_paths() ở trên (object Python mang uk/shareid, không
        # serialize qua JSON được). CHỈ truyền fs_id của các file .mp4 đã
        # lọc — TUYỆT ĐỐI KHÔNG fs_id của thư mục mẹ (né lỗi Baidu 130). Xem
        # docstring `_call_transfer_shared_paths` để biết chữ ký thật đã xác
        # nhận qua lỗi thực tế trên production.
        # ------------------------------------------------------------- #
        _dump_transfer_shared_paths_source(api)
        _transfer_results, skipped_fs_ids = _call_transfer_shared_paths(
            api, dest_dir, mp4_entries, share_url,
            uk_override=root_uk, share_id_override=root_share_id, bdstoken_override=root_bdstoken,
        )

        # CHỈ tính là "đã lưu" (saved_paths) những entry KHÔNG nằm trong
        # skipped_fs_ids — đảm bảo callback KHÔNG bao giờ báo về 1 file thực
        # ra chưa hề transfer thành công lên Cloud (do batch của nó bị BỎ QUA
        # sau khi retry hết BAIDU_TRANSFER_MAX_RETRIES lần, xem BATCH
        # ISOLATION trong `_call_transfer_shared_paths`).
        transferred_entries = [
            e for e in mp4_entries if getattr(e, "fs_id", None) not in skipped_fs_ids
        ]

        # FIX (log thực tế 14/08/2026, xem docstring `_list_personal_cloud_mp4s`):
        # KHÔNG dùng lại `_entry_path(e)` của `transferred_entries` nữa — đó là
        # path TRONG SHARE CỦA NGƯỜI KHÁC (trước transfer), không tồn tại
        # trong Cloud cá nhân -> resolve dlink 100% ra errno=-9. Phải quét lại
        # `dest_dir` trên Cloud cá nhân SAU transfer để lấy path thật.
        saved_paths = _list_personal_cloud_mp4s(bduss, stoken, root_bdstoken, dest_dir)
        saved_paths.sort(key=lambda p: _natural_sort_key({"path": p}))

        if not saved_paths:
            raise BaiduDownloadError(
                f"Transfer {drama_id} báo có kết quả nhưng quét lại {dest_dir} "
                "trên Cloud cá nhân KHÔNG thấy file .mp4 nào — có thể transfer "
                "thực ra thất bại hết, hoặc dest_dir bị lệch."
            )

        if skipped_fs_ids:
            logger.warning(
                "Transfer THÀNH CÔNG MỘT PHẦN — %d/%d file .mp4 đã lưu vào %s "
                "(drama_id=%s), %d file bị BỎ QUA do batch transfer lỗi (xem "
                "log ERROR phía trên để biết batch nào).",
                len(saved_paths), len(mp4_entries), dest_dir, drama_id,
                len(mp4_entries) - len(saved_paths),
            )
        else:
            logger.info(
                "Transfer thành công — %d file .mp4 đã lưu vào %s (drama_id=%s)",
                len(saved_paths), dest_dir, drama_id,
            )

        # FIX: resolve dlink NGAY TRONG process GH Actions (xem docstring
        # `_resolve_download_links`) — file nào không lấy được dlink bị loại
        # khỏi `saved_paths` cuối cùng luôn, để HF Space không tưởng nhầm là
        # "đã lưu" nhưng thật ra không có URL để tải.
        download_urls = _resolve_download_links(
            api, saved_paths, bduss=bduss, stoken=stoken, bdstoken=root_bdstoken,
        )
        saved_paths = [p for p in saved_paths if p in download_urls]
        if not saved_paths:
            raise BaiduDownloadError(
                f"Transfer {drama_id} có kết quả nhưng KHÔNG resolve được "
                "dlink cho BẤT KỲ file nào — kiểm tra log phía trên."
            )

        _send_callback(
            callback_url, webhook_secret, job_id,
            {
                "status": "success",
                "dest_dir": dest_dir,
                "drama_id": drama_id,
                "saved_paths": saved_paths,
                "download_urls": download_urls,
                # "partial": True nếu 1 vài batch transfer bị BỎ QUA (soft
                # fail) — HF Space vẫn xử lý bình thường với saved_paths hiện
                # có, chỉ là ÍT HƠN tổng số file .mp4 gốc đã tìm thấy.
                "partial": bool(skipped_fs_ids),
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
