"""
V2Ray/Xray Subscription Optimizer with Tkinter GUI
-------------------------------------------------
این برنامه لینک‌های subscription را دریافت کرده، کانفیگ‌ها را استخراج می‌کند،
تست اتصال/تاخیر را به صورت موازی انجام می‌دهد، و بهترین کانفیگ‌ها را رتبه‌بندی و خروجی می‌دهد.

Run with: python v2ray_optimizer_gui.py
Python 3.8+
"""

import asyncio
import base64
import json
import os
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ----------------------------- Data Models -----------------------------


@dataclass
class ConfigItem:
    raw: str
    protocol: str
    host: str
    port: int
    remark: str = ""
    extra: dict = field(default_factory=dict)

    def key(self):
        return f"{self.protocol}:{self.host}:{self.port}:{self.remark}"


@dataclass
class TestResult:
    config: ConfigItem
    success_count: int
    fail_count: int
    latencies: List[float]

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return (self.success_count / total) * 100 if total else 0.0

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else float("inf")

    @property
    def stability_score(self) -> float:
        # Higher is better: success rate / avg latency (ms)
        if not self.latencies:
            return 0
        return self.success_rate / (self.avg_latency + 1e-3)


# ----------------------------- Settings -----------------------------


DEFAULT_SETTINGS = {
    "subscriptions": [],
    "output_name": "best-configs",
    "top_n": 10,
    "tests_per_config": 20,
    "max_concurrency": 20,
    "timeout": 5,
    "xray_path": "",
}


CONFIG_FILE = "config.json"


def load_settings():
    if not os.path.exists(CONFIG_FILE):
        return DEFAULT_SETTINGS.copy()
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = DEFAULT_SETTINGS.copy()
        merged.update(data)
        return merged
    except Exception:
        return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print("Failed to save settings", exc)


# ----------------------------- Subscription Parsing -----------------------------


def _maybe_b64_decode(text: str) -> str:
    try:
        padded = text.strip()
        missing = len(padded) % 4
        if missing:
            padded += "=" * (4 - missing)
        decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
        if any(proto in decoded for proto in ("vmess://", "vless://", "trojan://", "ss://")):
            return decoded
    except Exception:
        pass
    return text


def parse_vmess(link: str) -> Optional[ConfigItem]:
    try:
        payload_b64 = link[len("vmess://"):]
        decoded = base64.b64decode(payload_b64 + "==").decode("utf-8")
        data = json.loads(decoded)
        host = data.get("add") or ""
        port = int(data.get("port") or 0)
        remark = data.get("ps") or data.get("name") or "vmess"
        return ConfigItem(raw=link.strip(), protocol="vmess", host=host, port=port, remark=remark, extra=data)
    except Exception:
        return None


def parse_url_like(link: str) -> Optional[ConfigItem]:
    # Handles vless://, trojan://, ss://
    try:
        from urllib.parse import urlparse

        parsed = urlparse(link)
        protocol = parsed.scheme
        host = parsed.hostname or ""
        port = parsed.port or 0
        remark = parsed.fragment or f"{protocol}"
        if not host or not port:
            return None
        return ConfigItem(raw=link.strip(), protocol=protocol, host=host, port=int(port), remark=remark)
    except Exception:
        return None


def parse_link(link: str) -> Optional[ConfigItem]:
    link = link.strip()
    if not link:
        return None
    if link.startswith("vmess://"):
        return parse_vmess(link)
    if link.startswith(("vless://", "trojan://", "ss://")):
        return parse_url_like(link)
    return None


def extract_links_from_text(content: str) -> List[str]:
    content = _maybe_b64_decode(content)
    lines = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        lines.append(line)
    return lines


def normalize_subscription_url(url: str) -> str:
    """Handle common cases like GitHub blob URLs -> raw."""
    if "github.com" in url and "/blob/" in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url


def fetch_subscription(url: str, timeout: int = 10) -> List[str]:
    try:
        real_url = normalize_subscription_url(url.strip())
        resp = requests.get(real_url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return extract_links_from_text(resp.text)
    except Exception:
        return []


# ----------------------------- Testing Engine -----------------------------


async def tcp_latency_test(host: str, port: int, timeout: float) -> Tuple[bool, float]:
    start = time.perf_counter()
    try:
        conn = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        end = time.perf_counter()
        return True, (end - start) * 1000
    except Exception:
        return False, float("inf")


async def test_config(config: ConfigItem, tests_per_config: int, timeout: float, sem: asyncio.Semaphore, progress_cb, log_cb):
    success = 0
    fails = 0
    latencies: List[float] = []
    for i in range(1, tests_per_config + 1):
        async with sem:
            ok, latency = await tcp_latency_test(config.host, config.port, timeout)
            if ok:
                success += 1
                latencies.append(latency)
                log_cb(f"[{config.remark}] تست {i}/{tests_per_config}: موفق - {latency:.1f} ms")
            else:
                fails += 1
                log_cb(f"[{config.remark}] تست {i}/{tests_per_config}: شکست (timeout یا اتصال ناموفق)")
            progress_cb()
    return TestResult(config=config, success_count=success, fail_count=fails, latencies=latencies)


async def run_all_tests(configs: List[ConfigItem], tests_per_config: int, timeout: float, max_concurrency: int, progress_cb, log_cb):
    sem = asyncio.Semaphore(max_concurrency)
    tasks = [test_config(cfg, tests_per_config, timeout, sem, progress_cb, log_cb) for cfg in configs]
    results: List[TestResult] = await asyncio.gather(*tasks)
    return results


# ----------------------------- Ranking & Output -----------------------------


def rank_results(results: List[TestResult]) -> List[TestResult]:
    # Sort by success rate desc, then avg latency asc, then stability desc
    return sorted(
        results,
        key=lambda r: (
            -r.success_rate,
            r.avg_latency,
            -r.stability_score,
        ),
    )


def save_output_files(best: List[TestResult], output_name: str):
    links = [res.config.raw for res in best]
    plain_path = f"{output_name}.txt"
    b64_path = f"{output_name}.base64"
    try:
        with open(plain_path, "w", encoding="utf-8") as f:
            f.write("\n".join(links))
    except Exception:
        pass
    try:
        encoded = base64.b64encode("\n".join(links).encode("utf-8")).decode("utf-8")
        with open(b64_path, "w", encoding="utf-8") as f:
            f.write(encoded)
    except Exception:
        pass


# ----------------------------- GUI -----------------------------


class V2RayOptimizerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("V2Ray/Xray Optimizer")
        self.root.geometry("900x700")

        self.settings = load_settings()

        self.log_queue = queue.Queue()
        self.progress_var = tk.DoubleVar(value=0)
        self.total_tests = 0
        self.completed_tests = 0
        self.tree_item_map = {}

        self._build_ui()
        self._load_settings_to_ui()
        self.root.after(300, self._process_log_queue)

    # ------------------------- UI Build -------------------------
    def _build_ui(self):
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        # Subscription Manager
        self.sub_tab = ttk.Frame(notebook)
        notebook.add(self.sub_tab, text="Subscription Manager")
        self._build_sub_tab()

        # Settings
        self.settings_tab = ttk.Frame(notebook)
        notebook.add(self.settings_tab, text="تنظیمات تست")
        self._build_settings_tab()

        # Run
        self.run_tab = ttk.Frame(notebook)
        notebook.add(self.run_tab, text="اجرای تست")
        self._build_run_tab()

        # Results
        self.results_tab = ttk.Frame(notebook)
        notebook.add(self.results_tab, text="نتایج")
        self._build_results_tab()

    def _build_sub_tab(self):
        ttk.Label(self.sub_tab, text="لینک‌های Subscription (هر خط یک URL)").pack(anchor="w", padx=10, pady=5)
        self.sub_text = tk.Text(self.sub_tab, height=10)
        self.sub_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        btn_frame = ttk.Frame(self.sub_tab)
        btn_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(btn_frame, text="بارگذاری از فایل", command=self._load_sub_from_file).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="پاک‌سازی", command=lambda: self.sub_text.delete("1.0", tk.END)).pack(side=tk.LEFT, padx=5)

    def _build_settings_tab(self):
        frm = ttk.Frame(self.settings_tab)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        def add_labeled(row, text, var):
            ttk.Label(frm, text=text).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(frm, textvariable=var, width=30)
            entry.grid(row=row, column=1, sticky="w", pady=4)
            return entry

        self.output_name_var = tk.StringVar()
        self.top_n_var = tk.IntVar()
        self.tests_per_cfg_var = tk.IntVar()
        self.max_conc_var = tk.IntVar()
        self.timeout_var = tk.DoubleVar()
        self.xray_path_var = tk.StringVar()

        add_labeled(0, "نام فایل خروجی", self.output_name_var)
        add_labeled(1, "تعداد کانفیگ برتر", self.top_n_var)
        add_labeled(2, "تعداد تست برای هر کانفیگ", self.tests_per_cfg_var)
        add_labeled(3, "تعداد Thread/Concurrency", self.max_conc_var)
        add_labeled(4, "Timeout هر تست (ثانیه)", self.timeout_var)
        xray_entry = add_labeled(5, "مسیر باینری xray/v2ray (اختیاری)", self.xray_path_var)

        ttk.Button(frm, text="انتخاب فایل باینری", command=lambda: self._browse_file(self.xray_path_var)).grid(row=5, column=2, padx=5)

        save_btn = ttk.Button(frm, text="ذخیره تنظیمات", command=self._save_settings)
        save_btn.grid(row=6, column=0, pady=10, sticky="w")
        load_btn = ttk.Button(frm, text="بارگذاری تنظیمات", command=self._load_settings_to_ui)
        load_btn.grid(row=6, column=1, pady=10, sticky="w")

    def _build_run_tab(self):
        frm = ttk.Frame(self.run_tab)
        frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.start_btn = ttk.Button(frm, text="شروع تست و بهینه‌سازی", command=self._on_start)
        self.start_btn.pack(pady=5)

        self.progress_bar = ttk.Progressbar(frm, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=5)

        self.status_label = ttk.Label(frm, text="وضعیت: آماده")
        self.status_label.pack(anchor="w", pady=5)

        ttk.Label(frm, text="لاگ زنده:").pack(anchor="w")
        self.log_text = tk.Text(frm, height=18)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _build_results_tab(self):
        columns = ("rank", "remark", "protocol", "avg_latency", "success_rate", "copy")
        self.tree = ttk.Treeview(self.results_tab, columns=columns, show="headings")
        self.tree.heading("rank", text="رتبه")
        self.tree.heading("remark", text="نام")
        self.tree.heading("protocol", text="پروتکل")
        self.tree.heading("avg_latency", text="میانگین تاخیر (ms)")
        self.tree.heading("success_rate", text="درصد موفقیت")
        self.tree.heading("copy", text="کپی کانفیگ")
        self.tree.column("copy", width=100, anchor="center")
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # رویداد کلیک برای ستون «کپی»
        self.tree.bind("<Button-1>", self._on_tree_click)

    # ------------------------- Helpers -------------------------
    def _browse_file(self, var: tk.StringVar):
        path = filedialog.askopenfilename()
        if path:
            var.set(path)

    def _load_sub_from_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.sub_text.insert(tk.END, content + "\n")
        except Exception as exc:
            messagebox.showerror("خطا", str(exc))

    def _save_settings(self):
        subs = [line.strip() for line in self.sub_text.get("1.0", tk.END).splitlines() if line.strip()]
        data = {
            "subscriptions": subs,
            "output_name": self.output_name_var.get() or DEFAULT_SETTINGS["output_name"],
            "top_n": int(self.top_n_var.get() or DEFAULT_SETTINGS["top_n"]),
            "tests_per_config": int(self.tests_per_cfg_var.get() or DEFAULT_SETTINGS["tests_per_config"]),
            "max_concurrency": int(self.max_conc_var.get() or DEFAULT_SETTINGS["max_concurrency"]),
            "timeout": float(self.timeout_var.get() or DEFAULT_SETTINGS["timeout"]),
            "xray_path": self.xray_path_var.get(),
        }
        save_settings(data)
        self._log("تنظیمات ذخیره شد")

    def _load_settings_to_ui(self):
        st = load_settings()
        self.sub_text.delete("1.0", tk.END)
        if st.get("subscriptions"):
            self.sub_text.insert(tk.END, "\n".join(st["subscriptions"]))
        self.output_name_var.set(st.get("output_name", DEFAULT_SETTINGS["output_name"]))
        self.top_n_var.set(st.get("top_n", DEFAULT_SETTINGS["top_n"]))
        self.tests_per_cfg_var.set(st.get("tests_per_config", DEFAULT_SETTINGS["tests_per_config"]))
        self.max_conc_var.set(st.get("max_concurrency", DEFAULT_SETTINGS["max_concurrency"]))
        self.timeout_var.set(st.get("timeout", DEFAULT_SETTINGS["timeout"]))
        self.xray_path_var.set(st.get("xray_path", ""))
        self._log("تنظیمات بارگذاری شد")

    def _log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {msg}\n")

    def _process_log_queue(self):
        try:
            while not self.log_queue.empty():
                msg = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, msg)
                self.log_text.see(tk.END)
        except Exception:
            pass
        self.root.after(300, self._process_log_queue)

    # ------------------------- Start Testing -------------------------
    def _on_start(self):
        try:
            subs = [line.strip() for line in self.sub_text.get("1.0", tk.END).splitlines() if line.strip()]
            if not subs:
                messagebox.showwarning("هشدار", "لطفا حداقل یک لینک subscription وارد کنید")
                return

            output_name = self.output_name_var.get() or DEFAULT_SETTINGS["output_name"]
            top_n = int(self.top_n_var.get() or DEFAULT_SETTINGS["top_n"])
            tests_per_config = int(self.tests_per_cfg_var.get() or DEFAULT_SETTINGS["tests_per_config"])
            max_concurrency = int(self.max_conc_var.get() or DEFAULT_SETTINGS["max_concurrency"])
            timeout = float(self.timeout_var.get() or DEFAULT_SETTINGS["timeout"])

            self.start_btn.config(state=tk.DISABLED)
            self.completed_tests = 0
            self.total_tests = 0
            self.progress_var.set(0)
            self.status_label.config(text="در حال جمع‌آوری کانفیگ‌ها...")

            thread = threading.Thread(
                target=self._worker,
                args=(subs, output_name, top_n, tests_per_config, max_concurrency, timeout),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            messagebox.showerror("خطا", str(exc))

    def _worker(self, subs, output_name, top_n, tests_per_config, max_concurrency, timeout):
        try:
            configs = self._collect_configs(subs)
            if not configs:
                self._log("هیچ کانفیگ معتبری پیدا نشد")
                self._finish()
                return

            self.total_tests = len(configs) * tests_per_config
            self._log(f"تعداد کانفیگ: {len(configs)} | کل تست‌ها: {self.total_tests}")
            self._set_status("در حال تست...")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            progress_cb = lambda: self._increment_progress()
            log_cb = lambda msg: self._log(msg)
            results: List[TestResult] = loop.run_until_complete(
                run_all_tests(configs, tests_per_config, timeout, max_concurrency, progress_cb, log_cb)
            )
            loop.close()

            ranked = rank_results(results)
            best = ranked[:top_n]
            save_output_files(best, output_name)
            self._update_results(best)
            self._set_status("اتمام تست")
            self._log(f"خروجی ذخیره شد: {output_name}.txt و {output_name}.base64")
        except Exception as exc:
            self._log("خطا: " + str(exc))
            traceback.print_exc()
        finally:
            self._finish()

    def _collect_configs(self, subs: List[str]) -> List[ConfigItem]:
        configs: List[ConfigItem] = []
        for url in subs:
            self._log(f"دریافت: {url}")
            links = fetch_subscription(url)
            for link in links:
                cfg = parse_link(link)
                if cfg:
                    configs.append(cfg)
        # Deduplicate
        unique = {}
        for cfg in configs:
            unique[cfg.key()] = cfg
        return list(unique.values())

    def _increment_progress(self):
        self.completed_tests += 1
        if self.total_tests:
            pct = (self.completed_tests / self.total_tests) * 100
            self.root.after(0, lambda: self.progress_var.set(pct))
            self.root.after(0, lambda: self.status_label.config(text=f"پیشرفت: {pct:.1f}%"))

    def _set_status(self, text: str):
        self.root.after(0, lambda: self.status_label.config(text=text))

    def _update_results(self, best: List[TestResult]):
        def update_tree():
            for row in self.tree.get_children():
                self.tree.delete(row)
            self.tree_item_map = {}
            # فقط کانفیگ‌هایی که حداقل یک موفقیت دارند نمایش داده می‌شوند
            filtered = [r for r in best if r.success_count > 0]
            for idx, res in enumerate(filtered, start=1):
                item_id = self.tree.insert(
                    "",
                    tk.END,
                    values=(
                        idx,
                        res.config.remark,
                        res.config.protocol,
                        f"{res.avg_latency:.1f}",
                        f"{res.success_rate:.1f}%",
                        "Copy",
                    ),
                )
                self.tree_item_map[item_id] = res.config.raw
        self.root.after(0, update_tree)

    def _on_tree_click(self, event):
        col = self.tree.identify_column(event.x)
        if col != "#6":  # ستون «کپی کانفیگ»
            return
        item = self.tree.identify_row(event.y)
        if not item:
            return
        raw = self.tree_item_map.get(item)
        if not raw:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(raw)
            self._log("کانفیگ در کلیپ‌بورد کپی شد")
        except Exception as exc:
            self._log(f"خطای کپی: {exc}")

    def _finish(self):
        self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))


def main():
    root = tk.Tk()
    app = V2RayOptimizerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
