"""HubSpot Company Matcher Worker — 从 pro1.py 适配为 Web 服务后端模块

核心逻辑不变，修改点：
- resolve_paths() 接受 base_dir 参数
- process_file() 增加 on_progress 回调
- 移除 CLI 入口（main/parse_args）
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urlparse

import pandas as pd
from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ============================================================
# 1. 固定配置
# ============================================================

HUBSPOT_PORTAL_ID = os.environ.get("HUBSPOT_PORTAL_ID", "9311334")
HUBSPOT_VIEW_ID = os.environ.get("HUBSPOT_VIEW_ID", "61027793")
HUBSPOT_SEARCH_URL = os.environ.get("HUBSPOT_SEARCH_URL",
    f"https://app.hubspot.com/contacts/{HUBSPOT_PORTAL_ID}/objects/0-2/views/{HUBSPOT_VIEW_ID}/list")
HUBSPOT_SEARCH_INPUT_SELECTOR = "input[data-test-id='crm-object-table-search-bar'][role='search']"
HUBSPOT_TABLE_SELECTOR = "table[data-test-id='framework-data-table']"
HUBSPOT_ROW_SELECTOR = "tbody tr[data-test-id^='row-']"
HUBSPOT_HEADER_SELECTOR = "thead th[data-column-index]"
HUBSPOT_SESSION_EXPIRED_SELECTOR = "[data-test-id='error-alert-SESSION_TIMED_OUT']"
HUBSPOT_EMPTY_STATE_SELECTORS = [
    "[class*='ZeroQueryResultsContainer']",
    "i18n-string[data-key='zeroQueryResults.generic.titleText']",
    "text=/No results|No companies|No matching records/i",
]
HUBSPOT_ERROR_STATE_SELECTORS = [
    "[data-test-id='error-alert']",
    "[role='alert'] >> text=/Something went wrong|Unable to load|Access denied/i",
]

NAME_COL_LABEL = os.environ.get("HUBSPOT_NAME_COL", "account name")
OWNER_COL_LABEL = os.environ.get("HUBSPOT_OWNER_COL", "account owner")
DOMAIN_COL_LABEL = os.environ.get("HUBSPOT_DOMAIN_COL", "account domain name")

WATCH_INTERVAL_SECONDS = 15
MAX_RETRY_PER_ROW = 2
SEARCH_TIMEOUT_SECONDS = 20
POST_FILL_SMALL_DELAY_SECONDS = 0.2
SEARCH_REACT_TIMEOUT_SECONDS = 8
STABILITY_POLL_SECONDS = 0.25
STABLE_ROUNDS = 3
EMPTY_CONFIRM_ROUNDS = 2
MIN_EMPTY_SETTLE_SECONDS = 1.0
PAGE_HEALTHCHECK_EVERY_N_SEARCHES = 20
MANUAL_LOGIN_WAIT_SECONDS = 300
STARTUP_AUTH_WAIT_SECONDS = 45
STARTUP_AUTH_POLL_SECONDS = 1
NAVIGATION_RETRY_TIMES = 3
NAVIGATION_RETRY_DELAY_SECONDS = 1.0
HEALTHCHECK_RESTART_DELAY_SECONDS = 1.5
AUTH_CHECK_MIN_INTERVAL_SECONDS = 1.5
STEALTH_CHROMIUM_ARGS = ["--disable-blink-features=AutomationControlled"]
STARTUP_SURFACE_READY_TIMEOUT_SECONDS = 20
COLUMN_MAP_RETRY_TIMES = 3
COLUMN_MAP_RETRY_DELAY_SECONDS = 0.8

LEGAL_SUFFIXES = {
    "ltd", "limited", "inc", "corp", "llc", "gmbh", "bv", "ag", "sa", "pte", "co", "company"
}
EMPTY_OWNER_VALUES = {"", "nan", "none", "null", "n/a", "-", "unassigned"}
VALID_INPUT_SUFFIXES = {".xlsx", ".xls", ".csv"}


# ============================================================
# 2. 数据结构
# ============================================================

@dataclass
class RuntimePaths:
    base_dir: Path
    workspace_dir: Path
    inbox_dir: Path
    results_dir: Path
    failed_dir: Path
    archive_dir: Path
    log_dir: Path
    profile_dir: Path
    log_file: Path


@dataclass
class Candidate:
    name: str
    owner: str
    domain: str


@dataclass
class MatchOutcome:
    status: str
    owner: str
    company_search_result_count: int
    domain_search_result_count: int


class SessionExpiredError(RuntimeError):
    pass


class FileProcessFatalError(RuntimeError):
    pass


# ============================================================
# 3. 工具函数
# ============================================================

def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_query_text(value: str) -> str:
    return unicodedata.normalize("NFC", normalize_spaces(value).casefold())


def encode_query_for_url(value: str) -> str:
    normalized = normalize_query_text(value)
    return quote_plus(normalized, safe="")


def repair_mojibake(text: str) -> str:
    if not text:
        return text
    markers_before = text.count("\u00c3") + text.count("\u00c2") + text.count("\u00e2")
    if markers_before == 0:
        return text
    try:
        fixed = text.encode("latin-1", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    markers_after = fixed.count("\u00c3") + fixed.count("\u00c2") + fixed.count("\u00e2")
    return fixed if markers_after < markers_before else text


def replace_punct_with_space(text: str) -> str:
    return re.sub(r"[^\w\s]", " ", text)


def build_company_search_key(name: Any) -> str:
    text = "" if name is None else str(name)
    text = repair_mojibake(text)
    text = replace_punct_with_space(text.lower())
    tokens = [token for token in text.split() if token not in LEGAL_SUFFIXES]
    return normalize_spaces(" ".join(tokens))


def build_company_match_key(name: Any) -> str:
    text = "" if name is None else str(name)
    text = repair_mojibake(text)
    text = replace_punct_with_space(text.lower())
    return normalize_spaces(text)


def build_domain_match_key(domain: Any) -> str:
    text = "" if domain is None else str(domain)
    text = text.strip().lower()
    if not text:
        return ""
    if not re.match(r"^[a-z]+://", text):
        text = "http://" + text
    parsed = urlparse(text)
    host = parsed.netloc or parsed.path
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    host = host.rstrip("/").rstrip(".")
    return host


def is_empty_owner(owner: Any) -> bool:
    if owner is None:
        return True
    text = str(owner).strip().lower()
    return text in EMPTY_OWNER_VALUES or text == ""


def safe_stem(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"[^\w\-. ]+", "_", stem)
    stem = re.sub(r"\s+", "_", stem)
    return stem.strip("_") or "result"


def build_output_path(results_dir: Path, src_name: str) -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return results_dir / f"{safe_stem(src_name)}_result_{ts}.xlsx"


def is_valid_input_file(path: Path) -> bool:
    return (
        path.is_file()
        and not path.name.startswith(".")
        and not path.name.startswith("~$")
        and path.suffix.lower() in VALID_INPUT_SUFFIXES
    )


def wait_until_file_stable(path: Path, timeout_seconds: int = 120, poll_seconds: int = 1, stable_rounds: int = 3) -> None:
    deadline = time.time() + timeout_seconds
    last_size: Optional[int] = None
    stable_hits = 0
    while time.time() < deadline:
        if not path.exists():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        if size > 0 and size == last_size:
            stable_hits += 1
            if stable_hits >= stable_rounds:
                return
        else:
            stable_hits = 0
            last_size = size
        time.sleep(poll_seconds)
    raise TimeoutError(f"input file not stable within timeout: {path.name}")


def is_target_closed_error(exc: Exception) -> bool:
    return "target page, context or browser has been closed" in str(exc).lower()


def is_transient_network_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = [
        "net::err_network_changed",
        "net::err_internet_disconnected",
        "net::err_connection_reset",
        "net::err_connection_closed",
        "net::err_connection_aborted",
        "net::err_connection_refused",
        "net::err_name_not_resolved",
        "net::err_timed_out",
    ]
    return any(marker in text for marker in markers)


# ============================================================
# 4. 路径和日志管理（适配 Web 服务）
# ============================================================

def resolve_paths(base_dir: Path) -> RuntimePaths:
    workspace_dir = base_dir / "workspace"
    inbox_dir = workspace_dir / "inbox"
    results_dir = workspace_dir / "results"
    failed_dir = workspace_dir / "failed"
    archive_dir = workspace_dir / "archive"
    log_dir = base_dir / "logs"
    profile_dir = base_dir / "chrome_profile"
    log_file = log_dir / "hubspot_worker.log"
    return RuntimePaths(
        base_dir=base_dir,
        workspace_dir=workspace_dir,
        inbox_dir=inbox_dir,
        results_dir=results_dir,
        failed_dir=failed_dir,
        archive_dir=archive_dir,
        log_dir=log_dir,
        profile_dir=profile_dir,
        log_file=log_file,
    )


def ensure_directories(paths: RuntimePaths) -> None:
    for path in [
        paths.workspace_dir,
        paths.inbox_dir,
        paths.results_dir,
        paths.failed_dir,
        paths.archive_dir,
        paths.log_dir,
        paths.profile_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def setup_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("hubspot_worker")
    logger.setLevel(logging.INFO)
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
        logger.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    sh = logging.StreamHandler()
    fh.setFormatter(fmt)
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ============================================================
# 5. Excel 文件处理
# ============================================================

class ExcelJob:
    def __init__(self, input_path: Path):
        self.input_path = input_path
        self.df_original = self._load_dataframe(input_path)
        self.company_col_idx = None
        self.domain_col_idx = None
        self._detect_columns()

    @staticmethod
    def _load_dataframe(path: Path) -> pd.DataFrame:
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path)
        return pd.read_excel(path)

    def _normalize_header(self, text: str) -> str:
        return re.sub(r"[^\w\s]", "", str(text).lower()).strip()

    def _detect_columns(self) -> None:
        if self.df_original.empty:
            raise FileProcessFatalError("input file is empty")
        if len(self.df_original.columns) == 0:
            raise FileProcessFatalError("input file has no columns")

        headers = [self._normalize_header(col) for col in self.df_original.columns]

        company_keywords = ["公司名", "company name", "company", "企业名", "accounts"]
        for keyword in company_keywords:
            for i, header in enumerate(headers):
                if keyword in header or header in keyword:
                    self.company_col_idx = i
                    break
            if self.company_col_idx is not None:
                break

        domain_keywords = ["域名", "domain", "网址", "website", "url"]
        for keyword in domain_keywords:
            for i, header in enumerate(headers):
                if keyword in header or header in keyword:
                    self.domain_col_idx = i
                    break
            if self.domain_col_idx is not None:
                break

        if self.company_col_idx is None and self.domain_col_idx is None:
            raise FileProcessFatalError(
                f"could not find company or domain columns. Available columns: {list(self.df_original.columns)}"
            )
        if self.company_col_idx is None:
            raise FileProcessFatalError(
                f"could not find company name column. Available columns: {list(self.df_original.columns)}"
            )
        if self.domain_col_idx is None:
            raise FileProcessFatalError(
                f"could not find domain column. Available columns: {list(self.df_original.columns)}"
            )

    def iter_rows(self):
        for idx, row in self.df_original.iterrows():
            yield idx, row

    def company_name(self, idx: int) -> str:
        value = self.df_original.iat[idx, self.company_col_idx]
        return "" if pd.isna(value) else str(value).strip()

    def domain(self, idx: int) -> str:
        value = self.df_original.iat[idx, self.domain_col_idx]
        return "" if pd.isna(value) else str(value).strip()


# ============================================================
# 6. HubSpot 浏览器控制
# ============================================================

class HubSpotBrowser:
    def __init__(self, paths: RuntimePaths, logger: logging.Logger):
        self.paths = paths
        self.logger = logger
        self.playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.search_count = 0
        self.column_index_map: Dict[str, str] = {}
        self.run_mode = "interactive"
        self._last_auth_check_ts = 0.0
        self._last_auth_state = "unknown"

    def start(self, run_mode: str = "background") -> None:
        self.run_mode = run_mode
        self.playwright = sync_playwright().start()
        headless = run_mode == "background"
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.paths.profile_dir),
            channel="chrome",
            headless=headless,
            ignore_default_args=["--enable-automation"],
            args=STEALTH_CHROMIUM_ARGS,
            slow_mo=0,
            viewport={"width": 1440, "height": 960},
        )
        self.context.add_init_script(
            """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
});
            """
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(12000)
        self.page.set_default_navigation_timeout(12000)
        self.goto_search_page()

        auth_state = self.wait_for_auth_ready(timeout_seconds=STARTUP_AUTH_WAIT_SECONDS)
        if auth_state != "ok":
            raise SessionExpiredError(
                f"HubSpot auth invalid on startup ({auth_state}). Run login to refresh session."
            )

        self._wait_startup_surface_ready(timeout_seconds=STARTUP_SURFACE_READY_TIMEOUT_SECONDS)
        self.refresh_column_index_map(with_retry=True, context="startup")
        self.logger.info("browser ready | run_mode=%s | headless=%s", run_mode, headless)

    def stop(self) -> None:
        for obj in [self.page, self.context, self.playwright]:
            try:
                if obj is not None:
                    obj.close() if hasattr(obj, "close") else obj.stop()
            except Exception as e:
                # 关闭失败可能泄漏浏览器进程；记一条但继续关其余对象
                self.logger.warning("关闭 %s 失败（可能泄漏进程）：%s",
                                    type(obj).__name__, e)
        self.page = None
        self.context = None
        self.playwright = None

    def login_bootstrap(self) -> None:
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.paths.profile_dir),
            channel="chrome",
            headless=False,
            ignore_default_args=["--enable-automation"],
            args=STEALTH_CHROMIUM_ARGS,
            slow_mo=0,
            viewport={"width": 1440, "height": 960},
        )
        self.context.add_init_script(
            """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
});
            """
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(0)
        self.page.set_default_navigation_timeout(0)
        self.goto_search_page()
        deadline = time.time() + MANUAL_LOGIN_WAIT_SECONDS
        self.logger.info("login bootstrap started | please login in the visible browser window")

        while time.time() < deadline:
            if self.detect_auth_state() == "ok":
                self.refresh_column_index_map()
                self.logger.info("login bootstrap completed | session saved to profile")
                return
            time.sleep(2)

        raise SessionExpiredError("manual login timeout reached; session still not valid")

    def goto_search_page(self) -> None:
        assert self.page is not None
        last_exc: Optional[Exception] = None
        for attempt in range(1, NAVIGATION_RETRY_TIMES + 1):
            try:
                self.page.goto(HUBSPOT_SEARCH_URL, wait_until="domcontentloaded")
                return
            except Exception as exc:
                last_exc = exc
                if is_transient_network_error(exc) and attempt < NAVIGATION_RETRY_TIMES:
                    self.logger.warning(
                        "goto_search_page transient error | attempt=%s/%s | error=%s",
                        attempt, NAVIGATION_RETRY_TIMES, exc,
                    )
                    time.sleep(NAVIGATION_RETRY_DELAY_SECONDS * attempt)
                    continue
                raise
        if last_exc is not None:
            raise last_exc

    def has_any_selector(self, selectors: List[str]) -> bool:
        assert self.page is not None
        for selector in selectors:
            try:
                if self.page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def detect_auth_state(self) -> str:
        assert self.page is not None
        current_url = self.page.url or ""

        try:
            if self.page.locator(HUBSPOT_SESSION_EXPIRED_SELECTOR).count() > 0:
                return "expired_alert"
        except Exception:
            pass

        url_lower = current_url.lower()
        login_tokens = ["accounts.hubspot.com/login", "/login", "signin", "session-expired"]
        if any(t in url_lower for t in login_tokens):
            return "login_page"

        try:
            search_count = self.page.locator(HUBSPOT_SEARCH_INPUT_SELECTOR).count()
            table_count = self.page.locator(HUBSPOT_TABLE_SELECTOR).count()
            if search_count > 0 and table_count > 0:
                return "ok"
            if search_count > 0 and "app.hubspot.com" in url_lower:
                return "ok"
        except Exception:
            pass

        try:
            if self.page.locator("input[type='email']").count() > 0 or self.page.locator("input[type='password']").count() > 0:
                return "login_page"
        except Exception:
            pass

        return "unknown"

    def wait_for_auth_ready(self, timeout_seconds: int) -> str:
        deadline = time.time() + timeout_seconds
        last_state = "unknown"
        while time.time() < deadline:
            state = self.detect_auth_state()
            last_state = state
            if state == "ok":
                return "ok"
            if state in {"login_page", "expired_alert"}:
                return state
            time.sleep(STARTUP_AUTH_POLL_SECONDS)
        return last_state

    def ensure_session_valid(self, min_interval_seconds: float = 0.0) -> None:
        now = time.time()
        if min_interval_seconds > 0 and (now - self._last_auth_check_ts) < min_interval_seconds and self._last_auth_state == "ok":
            return
        state = self.detect_auth_state()
        if state == "unknown":
            # 页面可能正在渲染中，如果 URL 还在 HubSpot 就多等一会
            url = (self.page.url or "").lower() if self.page else ""
            wait_time = 15 if "app.hubspot.com" in url else 4
            state = self.wait_for_auth_ready(timeout_seconds=wait_time)
        self._last_auth_check_ts = now
        self._last_auth_state = state
        if state != "ok":
            raise SessionExpiredError(f"HubSpot auth invalid during run ({state}).")

    def verify_page_health(self, force_reload: bool = False) -> None:
        assert self.page is not None
        try:
            if force_reload:
                self.goto_search_page()
            self.ensure_session_valid()
            self._wait_search_surface_ready(timeout_ms=10000)
        except Exception as exc:
            if not (is_target_closed_error(exc) or is_transient_network_error(exc)):
                raise
            self.logger.warning("page health failure | rebuilding browser | error=%s", exc)
            self.stop()
            time.sleep(HEALTHCHECK_RESTART_DELAY_SECONDS)
            self.start(run_mode=self.run_mode)

    def _wait_search_surface_ready(self, timeout_ms: int) -> None:
        assert self.page is not None
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            if self.detect_auth_state() == "ok":
                try:
                    if self.page.locator(HUBSPOT_TABLE_SELECTOR).count() > 0:
                        return
                except Exception:
                    pass
                if self.has_empty_state():
                    return
            time.sleep(0.2)
        raise RuntimeError("search page is not ready")

    def _wait_startup_surface_ready(self, timeout_seconds: int) -> None:
        assert self.page is not None
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            state = self.detect_auth_state()
            if state == "ok":
                try:
                    search_count = self.page.locator(HUBSPOT_SEARCH_INPUT_SELECTOR).count()
                    table_count = self.page.locator(HUBSPOT_TABLE_SELECTOR).count()
                    header_count = self.page.locator(HUBSPOT_HEADER_SELECTOR).count()
                    if search_count > 0 and (table_count > 0 or self.has_empty_state()) and header_count > 0:
                        return
                except Exception:
                    pass
            time.sleep(0.3)
        raise RuntimeError("startup surface did not become ready before timeout")

    def table(self) -> Locator:
        assert self.page is not None
        return self.page.locator(HUBSPOT_TABLE_SELECTOR).first

    def rows(self) -> Locator:
        return self.table().locator(HUBSPOT_ROW_SELECTOR)

    def _label_key(self, text: str) -> str:
        return normalize_spaces(replace_punct_with_space(text.lower()))

    def _collect_column_index_map(self) -> Dict[str, str]:
        headers = self.table().locator(HUBSPOT_HEADER_SELECTOR)
        count = headers.count()
        mapping: Dict[str, str] = {}
        for i in range(count):
            th = headers.nth(i)
            try:
                idx = th.get_attribute("data-column-index")
                label = th.inner_text(timeout=600)
            except Exception:
                continue
            if idx and label:
                mapping[self._label_key(label)] = idx
        return mapping

    def refresh_column_index_map(self, with_retry: bool = False, context: str = "runtime") -> None:
        attempts = COLUMN_MAP_RETRY_TIMES if with_retry else 1
        last_exc: Optional[Exception] = None
        for attempt in range(1, attempts + 1):
            try:
                mapping = self._collect_column_index_map()
                needed = [
                    self._label_key(NAME_COL_LABEL),
                    self._label_key(OWNER_COL_LABEL),
                    self._label_key(DOMAIN_COL_LABEL),
                ]
                missing = [key for key in needed if key not in mapping]
                if missing:
                    raise FileProcessFatalError(
                        f"failed to resolve HubSpot column index for headers: {', '.join(missing)}"
                    )
                self.column_index_map = mapping
                return
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    "column map refresh failed | context=%s | attempt=%s/%s | error=%s",
                    context, attempt, attempts, exc,
                )
                if attempt < attempts:
                    time.sleep(COLUMN_MAP_RETRY_DELAY_SECONDS * attempt)
        if last_exc is not None:
            raise last_exc

    def ensure_column_map_ready(self) -> None:
        needed = [self._label_key(NAME_COL_LABEL), self._label_key(OWNER_COL_LABEL), self._label_key(DOMAIN_COL_LABEL)]
        if self.column_index_map and all(key in self.column_index_map for key in needed):
            return
        self.verify_page_health(force_reload=True)
        self.refresh_column_index_map(with_retry=True, context="runtime_rebuild")

    def _column_idx(self, logical_label: str) -> str:
        return self.column_index_map[self._label_key(logical_label)]

    def has_empty_state(self) -> bool:
        return self.has_any_selector(HUBSPOT_EMPTY_STATE_SELECTORS)

    def has_error_state(self) -> bool:
        return self.has_any_selector(HUBSPOT_ERROR_STATE_SELECTORS)

    def _row_signature(self) -> str:
        try:
            if self.page.locator(HUBSPOT_TABLE_SELECTOR).count() == 0:
                return "rows:0"
        except Exception:
            return "rows:0"
        rows = self.rows()
        total = rows.count()
        if total == 0:
            return "rows:0"
        parts: List[str] = [f"rows:{total}"]
        name_idx = self._column_idx(NAME_COL_LABEL)
        for i in range(min(total, 5)):
            row = rows.nth(i)
            try:
                rid = row.get_attribute("data-test-id") or ""
                name = row.locator(f"td[data-column-index='{name_idx}']").first.inner_text(timeout=500).strip()
                parts.append(rid)
                parts.append(name)
            except Exception:
                pass
        return "|".join(parts)

    def _is_expected_query_applied(self, expected_query: str) -> bool:
        assert self.page is not None
        search_box = self.page.locator(HUBSPOT_SEARCH_INPUT_SELECTOR).first
        current_query = (search_box.input_value() or "").strip()
        if current_query.strip().lower() == expected_query.strip().lower():
            return True
        return self._is_expected_query_in_url(expected_query)

    def _is_expected_query_in_url(self, expected_query: str) -> bool:
        assert self.page is not None
        expected_plain = normalize_query_text(expected_query)
        if not expected_plain:
            return False
        try:
            current_url = self.page.url or ""
        except Exception:
            return False
        try:
            parsed = urlparse(current_url)
            query_values = parse_qs(parsed.query).get("query", [])
            for value in query_values:
                if normalize_query_text(value) == expected_plain:
                    return True
        except Exception:
            pass
        expected_plus = encode_query_for_url(expected_query)
        if not expected_plus:
            return False
        url_lower = current_url.lower()
        expected_percent = expected_plus.replace("+", "%20")
        return any(("query=" + v) in url_lower for v in {expected_plus, expected_percent} if v)

    def _wait_search_react(self, query: str) -> None:
        assert self.page is not None
        deadline = time.time() + SEARCH_REACT_TIMEOUT_SECONDS
        encoded_query_fragment = query.strip().lower().replace(" ", "%20")[:20]
        try:
            initial_url = (self.page.url or "").lower()
        except Exception:
            initial_url = ""

        while time.time() < deadline:
            try:
                current_url = (self.page.url or "").lower()
            except Exception:
                current_url = ""
            if encoded_query_fragment and encoded_query_fragment in current_url:
                return
            if current_url != initial_url:
                return
            try:
                table_locator = self.page.locator(HUBSPOT_TABLE_SELECTOR).first
                if self.page.locator(HUBSPOT_TABLE_SELECTOR).count() > 0:
                    loading = table_locator.get_attribute("data-test-loading", timeout=500) or "false"
                    if loading == "true" and self._is_expected_query_in_url(query):
                        return
                else:
                    if self._is_expected_query_in_url(query):
                        return
            except Exception:
                pass
            time.sleep(0.15)

    def _wait_search_result_ready(self, expected_query: str) -> str:
        deadline = time.time() + SEARCH_TIMEOUT_SECONDS
        started_at = time.time()
        saw_loading = False
        stable_hits = 0
        empty_hits = 0
        no_table_hits = 0
        last_signature: Optional[str] = None

        while time.time() < deadline:
            self.ensure_session_valid(min_interval_seconds=AUTH_CHECK_MIN_INTERVAL_SECONDS)

            if self.has_error_state():
                return "error"

            try:
                table_visible = self.page.locator(HUBSPOT_TABLE_SELECTOR).count() > 0
            except Exception:
                table_visible = False

            if not table_visible:
                no_table_hits += 1
                saw_loading = True
                stable_hits = 0
                last_signature = None
                query_applied = self._is_expected_query_in_url(expected_query)
                if query_applied and self.has_empty_state():
                    empty_hits += 1
                    if empty_hits >= EMPTY_CONFIRM_ROUNDS:
                        return "empty"
                elif query_applied and no_table_hits >= 20 and (time.time() - started_at) >= 5.0:
                    return "empty"
                time.sleep(STABILITY_POLL_SECONDS)
                continue

            no_table_hits = 0
            table_locator = self.page.locator(HUBSPOT_TABLE_SELECTOR).first
            loading_attr = table_locator.get_attribute("data-test-loading") or "false"
            if loading_attr == "true":
                saw_loading = True
                stable_hits = 0
                empty_hits = 0
                last_signature = None
                time.sleep(STABILITY_POLL_SECONDS)
                continue

            signature = self._row_signature()
            query_applied = self._is_expected_query_applied(expected_query)

            if query_applied and self.has_empty_state():
                if not saw_loading and (time.time() - started_at) < MIN_EMPTY_SETTLE_SECONDS:
                    time.sleep(STABILITY_POLL_SECONDS)
                    continue
                empty_hits += 1
                if empty_hits >= EMPTY_CONFIRM_ROUNDS:
                    return "empty"
            elif query_applied and signature == "rows:0":
                if not saw_loading and (time.time() - started_at) < MIN_EMPTY_SETTLE_SECONDS:
                    time.sleep(STABILITY_POLL_SECONDS)
                    continue
                empty_hits += 1
                if empty_hits >= EMPTY_CONFIRM_ROUNDS:
                    return "empty_fallback"
            else:
                empty_hits = 0

            if signature == last_signature:
                stable_hits += 1
            else:
                stable_hits = 0
                last_signature = signature

            if stable_hits >= STABLE_ROUNDS:
                if signature == "rows:0":
                    return "empty_fallback"
                return "ready"

            time.sleep(STABILITY_POLL_SECONDS)

        raise RuntimeError("search result did not stabilize within timeout")

    def _extract_text(self, locator: Locator, timeout_ms: int = 500) -> str:
        try:
            if locator.count() == 0:
                return ""
            return locator.inner_text(timeout=timeout_ms).strip()
        except Exception:
            return ""

    def _extract_owner(self, cell: Locator) -> str:
        primary = cell.locator("[data-test-id='truncated-object-label'][tabindex='0']").first
        text = self._extract_text(primary)
        if not text:
            text = self._extract_text(cell.locator("[data-test-id='truncated-object-label']").last)
        text = re.sub(r"\s*\([^)]*@[^)]*\)\s*$", "", text).strip()
        return text

    def _extract_name(self, cell: Locator) -> str:
        locators = [
            cell.locator("span[data-test-id^='label-cell-formatted-property-']").first,
            cell.locator("a [data-test-id='truncated-object-label']").last,
            cell.locator("[data-test-id='truncated-object-label']").last,
        ]
        for loc in locators:
            text = self._extract_text(loc)
            if text:
                return text
        return self._extract_text(cell)

    def _extract_domain(self, cell: Locator) -> str:
        link = cell.locator("a").first
        text = self._extract_text(link)
        if text:
            return text
        try:
            href = link.get_attribute("href") if link.count() > 0 else None
        except Exception:
            href = None
        if href:
            href = re.sub(r"^https?://", "", href, flags=re.I)
            return href.split("/")[0].strip()
        return self._extract_text(cell)

    def search(self, query: str) -> List[Candidate]:
        assert self.page is not None
        if self.search_count > 0 and PAGE_HEALTHCHECK_EVERY_N_SEARCHES > 0 and self.search_count % PAGE_HEALTHCHECK_EVERY_N_SEARCHES == 0:
            self.verify_page_health(force_reload=True)
            self.refresh_column_index_map()
        else:
            self.verify_page_health(force_reload=False)

        self.ensure_column_map_ready()

        if not query:
            return []

        search_box = self.page.locator(HUBSPOT_SEARCH_INPUT_SELECTOR).first
        search_box.click()
        try:
            search_box.press("Meta+A")
        except Exception:
            try:
                search_box.press("Control+A")
            except Exception:
                pass

        search_box.fill("")
        time.sleep(POST_FILL_SMALL_DELAY_SECONDS)
        search_box.fill(query)
        try:
            search_box.press("Enter")
        except Exception:
            pass

        self._wait_search_react(query)
        result_state = self._wait_search_result_ready(query)
        self.search_count += 1

        if result_state in {"empty", "empty_fallback"}:
            return []
        if result_state == "error":
            raise RuntimeError("HubSpot page entered error state while searching")

        rows = self.rows()
        total = rows.count()
        if total == 0:
            return []

        name_idx = self._column_idx(NAME_COL_LABEL)
        owner_idx = self._column_idx(OWNER_COL_LABEL)
        domain_idx = self._column_idx(DOMAIN_COL_LABEL)

        out: List[Candidate] = []
        for i in range(total):
            row = rows.nth(i)
            try:
                name_cell = row.locator(f"td[data-column-index='{name_idx}']").first
                owner_cell = row.locator(f"td[data-column-index='{owner_idx}']").first
                domain_cell = row.locator(f"td[data-column-index='{domain_idx}']").first
                if name_cell.count() == 0:
                    continue
                name = self._extract_name(name_cell)
                owner = self._extract_owner(owner_cell) if owner_cell.count() > 0 else ""
                domain = self._extract_domain(domain_cell) if domain_cell.count() > 0 else ""
            except Exception:
                continue
            out.append(Candidate(name=name, owner=owner, domain=domain))
        return out


# ============================================================
# 7. 匹配逻辑
# ============================================================

def exact_match(candidates: List[Candidate], company_match_key: str, domain_match_key: str) -> Tuple[str, Optional[Candidate]]:
    domain_hits: List[Candidate] = []
    name_hits: List[Candidate] = []

    for candidate in candidates:
        row_domain = build_domain_match_key(candidate.domain)
        row_company = build_company_match_key(candidate.name)
        if domain_match_key and row_domain == domain_match_key:
            domain_hits.append(candidate)
        if company_match_key and row_company == company_match_key:
            name_hits.append(candidate)

    if len(domain_hits) == 1:
        return "matched", domain_hits[0]
    if len(domain_hits) > 1:
        return "multiple_exact_matches", None
    if len(name_hits) == 1:
        return "matched", name_hits[0]
    if len(name_hits) > 1:
        return "multiple_exact_matches", None
    return "no_exact_match", None


def process_one(browser: HubSpotBrowser, company: str, domain: str) -> MatchOutcome:
    company_search_key = build_company_search_key(company)
    company_match_key = build_company_match_key(company)
    domain_match_key = build_domain_match_key(domain)

    first_round = browser.search(company_search_key) if company_search_key else []
    company_count = len(first_round)
    status_1, candidate_1 = exact_match(first_round, company_match_key, domain_match_key)

    if status_1 == "matched":
        owner = "" if candidate_1 is None else candidate_1.owner
        if is_empty_owner(owner):
            return MatchOutcome("matched_owner_empty", "", company_count, 0)
        return MatchOutcome("matched", owner, company_count, 0)

    if status_1 == "multiple_exact_matches":
        return MatchOutcome("multiple_exact_matches", "", company_count, 0)

    second_round = browser.search(domain_match_key) if domain_match_key else []
    domain_count = len(second_round)
    status_2, candidate_2 = exact_match(second_round, company_match_key, domain_match_key)

    if status_2 == "matched":
        owner = "" if candidate_2 is None else candidate_2.owner
        if is_empty_owner(owner):
            return MatchOutcome("matched_owner_empty", "", company_count, domain_count)
        return MatchOutcome("matched", owner, company_count, domain_count)

    if status_2 == "multiple_exact_matches":
        return MatchOutcome("multiple_exact_matches", "", company_count, domain_count)

    return MatchOutcome("no_match", "", company_count, domain_count)


# ============================================================
# 8. 文件处理（增加 on_progress 回调）
# ============================================================

def process_file(
    browser: HubSpotBrowser,
    input_path: Path,
    paths: RuntimePaths,
    logger: logging.Logger,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    wait_until_file_stable(input_path)
    job = ExcelJob(input_path)
    output_rows: List[Dict[str, Any]] = []
    total_rows = len(job.df_original)

    logger.info("start file | name=%s | rows=%s", input_path.name, total_rows)
    if on_progress:
        on_progress(0, total_rows)

    for idx, _row in job.iter_rows():
        company = job.company_name(idx)
        domain = job.domain(idx)

        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRY_PER_ROW + 2):
            try:
                outcome = process_one(browser, company, domain)
                output_rows.append({
                    "company": company,
                    "domain": domain,
                    "status": outcome.status,
                    "owner": outcome.owner,
                    "company_search_result_count": outcome.company_search_result_count,
                    "domain_search_result_count": outcome.domain_search_result_count,
                })
                logger.info(
                    "row done | row=%s | company=%s | status=%s",
                    idx, company, outcome.status,
                )
                last_error = None
                break
            except FileProcessFatalError:
                raise
            except SessionExpiredError as exc:
                if attempt <= MAX_RETRY_PER_ROW:
                    logger.warning("session expired at row %s, attempt %s | reloading page to recover", idx, attempt)
                    try:
                        browser.stop()
                        time.sleep(2)
                        browser.start(run_mode=browser.run_mode)
                        browser.refresh_column_index_map(with_retry=True, context="session_recovery")
                        logger.info("session recovered after reload")
                        continue
                    except Exception:
                        raise exc
                raise
            except Exception as exc:
                last_error = exc
                logger.warning("row retry | row=%s | attempt=%s | error=%s", idx, attempt, exc)
                try:
                    browser.verify_page_health(force_reload=True)
                except Exception as heal_exc:
                    if not is_transient_network_error(heal_exc):
                        raise
                try:
                    browser.refresh_column_index_map(with_retry=True, context="row_retry")
                except Exception:
                    pass
                time.sleep(1)

        if last_error is not None:
            err_text = str(last_error)
            recoverable = is_transient_network_error(last_error) or "search result did not stabilize" in err_text
            if recoverable:
                logger.error("row skipped after retries | row=%s | company=%s | error=%s", idx, company, last_error)
                output_rows.append({
                    "company": company,
                    "domain": domain,
                    "status": "row_skipped_after_retries",
                    "owner": "",
                    "company_search_result_count": 0,
                    "domain_search_result_count": 0,
                })
            else:
                raise FileProcessFatalError(f"row failed | row={idx} | company={company} | error={last_error}")

        if on_progress:
            on_progress(idx + 1, total_rows)

    output_path = build_output_path(paths.results_dir, input_path.name)
    pd.DataFrame(output_rows).to_excel(output_path, index=False)
    logger.info("file success | input=%s | output=%s", input_path.name, output_path.name)
    return output_path
