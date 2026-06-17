"""
V2Ray/Xray Subscription Optimizer - Modern GUI
-------------------------------------------------
این برنامه لینک‌های subscription را دریافت کرده، کانفیگ‌ها را استخراج می‌کند،
تست اتصال/تاخیر را به صورت موازی و بهینه انجام می‌دهد، و بهترین کانفیگ‌ها را رتبه‌بندی و خروجی می‌دهد.

Run with: python v2ray_optimizer_modern.py
Python 3.8+
"""

import asyncio
import base64
import json
import os
import queue
import socket
import subprocess
import tempfile
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests
import urllib3
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font

# Disable SSL warnings for faster testing
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ----------------------------- Country Detection -----------------------------


_country_cache = {}  # Cache for country lookups


def get_country_from_ip(ip: str) -> str:
    """Get country code from IP address using free API (with caching)"""
    if ip in _country_cache:
        return _country_cache[ip]
    
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}", timeout=1)
        data = response.json()
        if data.get("status") == "success":
            country_code = data.get("countryCode", "??")
            country_name = data.get("country", "Unknown")
            result = f"{country_code} ({country_name})"
            _country_cache[ip] = result
            return result
        result = "??"
        _country_cache[ip] = result
        return result
    except:
        result = "??"
        _country_cache[ip] = result
        return result

# ----------------------------- Modern Theme -----------------------------


class ModernTheme:
    """Modern dark theme with RTL support"""
    
    # Color Palette
    BG_PRIMARY = "#1e1e2e"
    BG_SECONDARY = "#313244"
    BG_TERTIARY = "#45475a"
    ACCENT = "#89b4fa"
    ACCENT_HOVER = "#b4befe"
    SUCCESS = "#a6e3a1"
    WARNING = "#f9e2af"
    ERROR = "#f38ba8"
    TEXT_PRIMARY = "#cdd6f4"
    TEXT_SECONDARY = "#a6adc8"
    BORDER = "#585b70"
    
    # Fonts
    FONT_FAMILY = "Segoe UI, Tahoma, Geneva, Verdana, sans-serif"
    FONT_SIZE_NORMAL = 10
    FONT_SIZE_LARGE = 12
    FONT_SIZE_TITLE = 14
    
    @classmethod
    def configure_style(cls, style: ttk.Style):
        """Configure modern ttk styles"""
        style.theme_use('clam')
        
        # Configure colors
        style.configure("TFrame", background=cls.BG_PRIMARY)
        style.configure("TLabel", background=cls.BG_PRIMARY, foreground=cls.TEXT_PRIMARY, 
                       font=(cls.FONT_FAMILY, cls.FONT_SIZE_NORMAL))
        style.configure("TButton", background=cls.ACCENT, foreground=cls.BG_PRIMARY,
                       font=(cls.FONT_FAMILY, cls.FONT_SIZE_NORMAL, "bold"),
                       borderwidth=0, focuscolor='none')
        style.map("TButton", background=[('active', cls.ACCENT_HOVER)])
        
        style.configure("TEntry", fieldbackground=cls.BG_SECONDARY, 
                       foreground=cls.TEXT_PRIMARY, insertcolor=cls.ACCENT,
                       borderwidth=1, relief="solid")
        
        style.configure("TNotebook", background=cls.BG_PRIMARY, borderwidth=0)
        style.configure("TNotebook.Tab", background=cls.BG_SECONDARY, 
                       foreground=cls.TEXT_SECONDARY, padding=[20, 10],
                       font=(cls.FONT_FAMILY, cls.FONT_SIZE_NORMAL))
        style.map("TNotebook.Tab", background=[('selected', cls.ACCENT)],
                 foreground=[('selected', cls.BG_PRIMARY)])
        
        style.configure("TProgressbar", background=cls.ACCENT, troughcolor=cls.BG_SECONDARY,
                       borderwidth=0, thickness=8)
        
        # Treeview styling
        style.configure("Treeview", background=cls.BG_SECONDARY, foreground=cls.TEXT_PRIMARY,
                       fieldbackground=cls.BG_SECONDARY, font=(cls.FONT_FAMILY, cls.FONT_SIZE_NORMAL),
                       rowheight=30)
        style.configure("Treeview.Heading", background=cls.BG_TERTIARY, 
                       foreground=cls.TEXT_PRIMARY, font=(cls.FONT_FAMILY, cls.FONT_SIZE_NORMAL, "bold"))
        style.map("Treeview", background=[('selected', cls.ACCENT)],
                 foreground=[('selected', cls.BG_PRIMARY)])
        
        # Scrollbar
        style.configure("Vertical.TScrollbar", background=cls.BG_TERTIARY,
                       troughcolor=cls.BG_SECONDARY, borderwidth=0)
        style.configure("Horizontal.TScrollbar", background=cls.BG_TERTIARY,
                       troughcolor=cls.BG_SECONDARY, borderwidth=0)


# ----------------------------- Data Models -----------------------------


@dataclass
class ConfigItem:
    raw: str
    protocol: str
    host: str
    port: int
    remark: str = ""
    extra: dict = field(default_factory=dict)
    country: str = ""

    def key(self):
        return f"{self.protocol}:{self.host}:{self.port}:{self.remark}"


@dataclass
class TestResult:
    config: ConfigItem
    success_count: int
    fail_count: int
    latencies: List[float]
    tcp_latencies: List[float] = field(default_factory=list)  # Store TCP latencies separately
    use_xray: bool = False

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return (self.success_count / total) * 100 if total else 0.0

    @property
    def avg_latency(self) -> float:
        return sum(self.latencies) / len(self.latencies) if self.latencies else float("inf")

    @property
    def min_latency(self) -> float:
        return min(self.latencies) if self.latencies else float("inf")

    @property
    def max_latency(self) -> float:
        return max(self.latencies) if self.latencies else float("inf")

    @property
    def avg_tcp_latency(self) -> float:
        return sum(self.tcp_latencies) / len(self.tcp_latencies) if self.tcp_latencies else float("inf")

    @property
    def stability_score(self) -> float:
        if not self.latencies:
            return 0
        # Lower standard deviation = more stable
        if len(self.latencies) < 2:
            return self.success_rate / (self.avg_latency + 1e-3)
        import statistics
        std_dev = statistics.stdev(self.latencies)
        return (self.success_rate * 100) / (self.avg_latency + std_dev + 1e-3)


# ----------------------------- Settings -----------------------------


DEFAULT_SETTINGS = {
    "subscriptions": [],
    "output_name": "best-configs",
    "top_n": 10,
    "tests_per_config": 5,
    "max_concurrency": 50,
    "timeout": 3,
    "xray_path": "",
    "dark_mode": True,
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
        # Don't get country during parsing to avoid blocking
        return ConfigItem(raw=link.strip(), protocol="vmess", host=host, port=port, remark=remark, extra=data, country="")
    except Exception:
        return None


def parse_url_like(link: str) -> Optional[ConfigItem]:
    try:
        from urllib.parse import urlparse
        parsed = urlparse(link)
        protocol = parsed.scheme
        host = parsed.hostname or ""
        port = parsed.port or 0
        remark = parsed.fragment or f"{protocol}"
        if not host or not port:
            return None
        # Don't get country during parsing to avoid blocking
        return ConfigItem(raw=link.strip(), protocol=protocol, host=host, port=int(port), remark=remark, country="")
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
        url = url.strip()
        
        # Check if it's a local file path
        if os.path.exists(url):
            with open(url, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Check if it's a JSON result file
            if url.endswith("_results.json"):
                try:
                    data = json.loads(content)
                    if "results" in data:
                        return [item["raw"] for item in data["results"] if "raw" in item]
                except:
                    pass
            
            return extract_links_from_text(content)
        
        # Otherwise treat as URL
        real_url = normalize_subscription_url(url)
        resp = requests.get(real_url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        return extract_links_from_text(resp.text)
    except Exception:
        return []


# ----------------------------- Optimized Testing Engine -----------------------------


def sync_tcp_latency_test(host: str, port: int, timeout: float) -> Tuple[bool, float]:
    """Synchronous TCP latency test using socket for better performance"""
    start = time.perf_counter()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.close()
        end = time.perf_counter()
        return True, (end - start) * 1000
    except Exception:
        return False, float("inf")


async def tcp_latency_test(host: str, port: int, timeout: float) -> Tuple[bool, float]:
    """Async wrapper for TCP latency test"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = await loop.run_in_executor(
            executor, sync_tcp_latency_test, host, port, timeout
        )
    return result


# ----------------------------- Xray Core Integration -----------------------------


def generate_xray_config(config: ConfigItem, local_port: int) -> dict:
    """Generate Xray config from ConfigItem"""
    xray_config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": local_port,
                "protocol": "socks",
                "settings": {"udp": True},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}
            }
        ],
        "outbounds": []
    }
    
    if config.protocol == "vmess":
        extra = config.extra
        vmess_out = {
            "protocol": "vmess",
            "settings": {
                "vnext": [
                    {
                        "address": config.host,
                        "port": config.port,
                        "users": [
                            {
                                "id": extra.get("id", ""),
                                "alterId": extra.get("aid", 0),
                                "security": extra.get("scy", "auto")
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {
                "network": extra.get("net", "tcp"),
                "security": extra.get("tls", "").lower() == "tls" or extra.get("tls", "") is True,
                "tlsSettings": {} if extra.get("tls", "").lower() == "tls" or extra.get("tls", "") is True else None,
                "wsSettings": {
                    "path": extra.get("path", "/"),
                    "headers": {"Host": extra.get("host", "")}
                } if extra.get("net", "") == "ws" else None,
                "grpcSettings": {
                    "serviceName": extra.get("serviceName", ""),
                    "multiMode": False
                } if extra.get("net", "") == "grpc" else None
            }
        }
        # Clean up None values
        if vmess_out["streamSettings"]["security"] == False:
            del vmess_out["streamSettings"]["security"]
            del vmess_out["streamSettings"]["tlsSettings"]
        if vmess_out["streamSettings"].get("wsSettings") is None:
            del vmess_out["streamSettings"]["wsSettings"]
        if vmess_out["streamSettings"].get("grpcSettings") is None:
            del vmess_out["streamSettings"]["grpcSettings"]
        xray_config["outbounds"].append(vmess_out)
    
    elif config.protocol in ["vless", "trojan"]:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(config.raw)
        
        outbound = {
            "protocol": config.protocol,
            "settings": {
                "vnext" if config.protocol == "vless" else "servers": [
                    {
                        "address": config.host,
                        "port": config.port,
                        "users": [
                            {
                                "id": parsed.username,
                                "encryption": "none",
                                "flow": parse_qs(parsed.query).get("flow", [""])[0]
                            }
                        ] if config.protocol == "vless" else [
                            {
                                "password": parsed.username
                            }
                        ]
                    }
                ]
            },
            "streamSettings": {
                "network": parse_qs(parsed.query).get("type", ["tcp"])[0],
                "security": parse_qs(parsed.query).get("security", ["none"])[0],
                "tlsSettings": {
                    "serverName": parse_qs(parsed.query).get("sni", [""])[0],
                    "allowInsecure": True
                } if parse_qs(parsed.query).get("security", ["none"])[0] == "tls" else None,
                "realitySettings": {
                    "serverName": parse_qs(parsed.query).get("sni", [""])[0],
                    "publicKey": parse_qs(parsed.query).get("pbk", [""])[0],
                    "shortId": parse_qs(parsed.query).get("sid", [""])[0],
                    "fingerprint": parse_qs(parsed.query).get("fp", ["chrome"])[0]
                } if parse_qs(parsed.query).get("security", ["none"])[0] == "reality" else None,
                "wsSettings": {
                    "path": parse_qs(parsed.query).get("path", ["/"])[0],
                    "headers": {"Host": parse_qs(parsed.query).get("host", [""])[0]}
                } if parse_qs(parsed.query).get("type", ["tcp"])[0] == "ws" else None,
                "grpcSettings": {
                    "serviceName": parse_qs(parsed.query).get("serviceName", [""])[0],
                    "multiMode": False
                } if parse_qs(parsed.query).get("type", ["tcp"])[0] == "grpc" else None
            }
        }
        
        # Clean up None values
        if outbound["streamSettings"]["security"] == "none":
            del outbound["streamSettings"]["security"]
            del outbound["streamSettings"]["tlsSettings"]
            del outbound["streamSettings"]["realitySettings"]
        if outbound["streamSettings"].get("tlsSettings") is None:
            del outbound["streamSettings"]["tlsSettings"]
        if outbound["streamSettings"].get("realitySettings") is None:
            del outbound["streamSettings"]["realitySettings"]
        if outbound["streamSettings"].get("wsSettings") is None:
            del outbound["streamSettings"]["wsSettings"]
        if outbound["streamSettings"].get("grpcSettings") is None:
            del outbound["streamSettings"]["grpcSettings"]
        
        xray_config["outbounds"].append(outbound)
    
    else:
        # Fallback for unsupported protocols - use direct outbound
        xray_config["outbounds"].append({
            "protocol": "freedom",
            "settings": {}
        })
    
    # Debug: save config for inspection
    try:
        debug_dir = "debug_configs"
        os.makedirs(debug_dir, exist_ok=True)
        safe_name = f"{config.protocol}_{config.host}_{config.port}".replace(":", "_").replace(".", "_")
        with open(os.path.join(debug_dir, f"{safe_name}.json"), "w", encoding="utf-8") as f:
            json.dump(xray_config, f, indent=2, ensure_ascii=False)
    except:
        pass
    
    return xray_config


def test_with_xray(config: ConfigItem, xray_path: str, timeout: float) -> Tuple[bool, float]:
    """Test config using Xray core with real HTTP request - returns (success, latency_ms)"""
    local_port = 2080 + (hash(config.key()) % 100)  # Random port between 2080-2179
    
    try:
        # Generate Xray config
        xray_config = generate_xray_config(config, local_port)
        
        # Write config to temp file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump(xray_config, f, indent=2)
            config_file = f.name
        
        process = None
        try:
            # Start Xray process
            process = subprocess.Popen(
                [xray_path, "-config", config_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            # Wait for Xray to start
            time.sleep(2.0)
            
            # Check if process is still running
            if process.poll() is not None:
                return False, float("inf")
            
            # Test with PySocks (more reliable)
            start = time.perf_counter()
            try:
                import socks
                proxies = {
                    'http': f'socks5://127.0.0.1:{local_port}',
                    'https': f'socks5://127.0.0.1:{local_port}'
                }
                
                # Test with fast endpoints
                test_urls = [
                    "http://cp.cloudflare.com/generate_204",
                    "http://www.gstatic.com/generate_204",
                    "http://connectivitycheck.gstatic.com/generate_204"
                ]
                
                success = False
                for url in test_urls:
                    try:
                        response = requests.get(url, proxies=proxies, timeout=timeout,
                                               headers={'User-Agent': 'Mozilla/5.0'},
                                               verify=False)
                        if response.status_code in [200, 204]:
                            success = True
                            break
                    except:
                        continue
                
                end = time.perf_counter()
                
                if success:
                    return True, (end - start) * 1000
                else:
                    # Fallback to manual SOCKS5
                    return _test_with_socks5_manual(local_port, timeout, start)
                    
            except ImportError:
                # Fallback to manual SOCKS5 implementation
                return _test_with_socks5_manual(local_port, timeout, start)
                
        finally:
            # Kill Xray process
            if process:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except:
                    process.kill()
            
            # Clean up temp file
            try:
                os.unlink(config_file)
            except:
                pass
                
    except Exception as e:
        return False, float("inf")


def _test_with_socks5_manual(local_port: int, timeout: float, start_time: float) -> Tuple[bool, float]:
    """Manual SOCKS5 implementation as fallback with proper HTTP request"""
    sock = None
    try:
        # Connect to local SOCKS proxy
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(("127.0.0.1", local_port))
        
        # SOCKS5 handshake
        sock.sendall(b'\x05\x01\x00')
        response = sock.recv(2)
        if len(response) < 2 or response[0] != 0x05 or response[1] != 0x00:
            return False, float("inf")
        
        # Connect to test server (Cloudflare 1.1.1.1)
        test_host = "1.1.1.1"
        test_port = 80
        
        # Build SOCKS5 connect request
        request = b'\x05\x01\x00\x01'  # VER, CMD, RSV, ATYP (IPv4)
        request += socket.inet_aton(test_host)
        request += test_port.to_bytes(2, 'big')
        
        sock.sendall(request)
        
        # Read SOCKS5 response
        response = sock.recv(10)
        if len(response) < 4 or response[0] != 0x05 or response[1] != 0x00:
            return False, float("inf")
        
        # Send HTTP GET request to Cloudflare connectivity check
        http_request = b"GET /generate_204 HTTP/1.1\r\n"
        http_request += b"Host: cp.cloudflare.com\r\n"
        http_request += b"User-Agent: Mozilla/5.0\r\n"
        http_request += b"Connection: close\r\n\r\n"
        
        sock.sendall(http_request)
        
        # Wait for HTTP response
        sock.settimeout(timeout)
        response = b""
        while True:
            chunk = sock.recv(1024)
            if not chunk:
                break
            response += chunk
            if b"\r\n\r\n" in response:
                break
        
        sock.close()
        end = time.perf_counter()
        
        # Check for valid HTTP response
        if b"HTTP/1.1 204" in response or b"HTTP/1.1 200" in response or b"HTTP/1.0" in response:
            return True, (end - start_time) * 1000
        else:
            return False, float("inf")
            
    except Exception as e:
        if sock:
            try:
                sock.close()
            except:
                pass
        return False, float("inf")


async def test_config(config: ConfigItem, tests_per_config: int, timeout: float, 
                     sem: asyncio.Semaphore, progress_cb, log_cb, xray_path: str = ""):
    success = 0
    fails = 0
    latencies: List[float] = []
    tcp_latencies: List[float] = []
    use_xray = bool(xray_path and os.path.exists(xray_path))
    
    for i in range(1, tests_per_config + 1):
        async with sem:
            # First do TCP test (always)
            tcp_ok, tcp_latency = await tcp_latency_test(config.host, config.port, timeout)
            if tcp_ok:
                tcp_latencies.append(tcp_latency)
            
            # Then do Xray test if available
            if use_xray:
                loop = asyncio.get_event_loop()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    xray_ok, xray_latency = await loop.run_in_executor(
                        executor, test_with_xray, config, xray_path, timeout
                    )
                
                if xray_ok:
                    success += 1
                    latencies.append(xray_latency)
                    log_cb(f"✓ [{config.remark}] تست {i}/{tests_per_config}: Proxy={xray_latency:.1f}ms | TCP={tcp_latency:.1f}ms")
                else:
                    fails += 1
                    if tcp_ok:
                        log_cb(f"⚠ [{config.remark}] تست {i}/{tests_per_config}: Proxy شکست (TCP={tcp_latency:.1f}ms)")
                    else:
                        log_cb(f"✗ [{config.remark}] تست {i}/{tests_per_config}: Proxy و TCP شکست")
            else:
                # Fallback to TCP only
                if tcp_ok:
                    success += 1
                    latencies.append(tcp_latency)
                    log_cb(f"✓ [{config.remark}] تست {i}/{tests_per_config}: TCP={tcp_latency:.1f}ms")
                else:
                    fails += 1
                    log_cb(f"✗ [{config.remark}] تست {i}/{tests_per_config}: شکست")
            
            progress_cb()
    
    return TestResult(config=config, success_count=success, fail_count=fails, 
                     latencies=latencies, tcp_latencies=tcp_latencies, use_xray=use_xray)


async def run_all_tests(configs: List[ConfigItem], tests_per_config: int, 
                       timeout: float, max_concurrency: int, progress_cb, log_cb, xray_path: str = ""):
    sem = asyncio.Semaphore(max_concurrency)
    tasks = [test_config(cfg, tests_per_config, timeout, sem, progress_cb, log_cb, xray_path) 
             for cfg in configs]
    results: List[TestResult] = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Filter out exceptions
    valid_results = []
    for r in results:
        if isinstance(r, TestResult):
            valid_results.append(r)
        else:
            log_cb(f"خطا در تست: {r}")
    
    return valid_results


# ----------------------------- Ranking & Output -----------------------------


def rank_results(results: List[TestResult]) -> List[TestResult]:
    """Sort by success rate desc, then avg latency asc, then stability desc"""
    return sorted(
        results,
        key=lambda r: (
            -r.success_rate,
            r.avg_latency,
            -r.stability_score,
        ),
    )


def auto_upload_to_github():
    """Automatically commit and push results to GitHub"""
    try:
        import threading
        def upload_task():
            try:
                # Add all changes
                subprocess.run(["git", "add", "."], 
                             capture_output=True, 
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                             timeout=30)
                
                # Commit with timestamp
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                subprocess.run(["git", "commit", "-m", f"Update test results - {timestamp}"],
                             capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                             timeout=30)
                
                # Push to GitHub
                subprocess.run(["git", "push", "-u", "origin", "main"],
                             capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                             timeout=60)
            except Exception as e:
                print(f"Git upload error: {e}")
        
        # Run in background thread to not block UI
        thread = threading.Thread(target=upload_task, daemon=True)
        thread.start()
    except Exception:
        pass


def save_output_files(best: List[TestResult], output_name: str):
    links = [res.config.raw for res in best]
    plain_path = f"{output_name}.txt"
    b64_path = f"{output_name}.base64"
    json_path = f"{output_name}_results.json"
    
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
    
    # Save detailed results to JSON for GitHub upload
    try:
        results_data = []
        for res in best:
            results_data.append({
                "remark": res.config.remark,
                "protocol": res.config.protocol,
                "host": res.config.host,
                "port": res.config.port,
                "country": res.config.country,
                "proxy_latency": res.avg_latency if res.latencies else None,
                "tcp_latency": res.avg_tcp_latency if res.tcp_latencies else None,
                "success_rate": res.success_rate,
                "min_latency": res.min_latency,
                "max_latency": res.max_latency,
                "use_xray": res.use_xray,
                "raw": res.config.raw
            })
        
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "total_configs": len(best),
                "results": results_data
            }, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    
    # Auto-upload to GitHub
    auto_upload_to_github()


# ----------------------------- Modern GUI -----------------------------


class ModernButton(tk.Canvas):
    """Custom modern button with hover effects"""
    
    def __init__(self, parent, text, command=None, bg_color=None, 
                 text_color=None, hover_color=None, width=120, height=35, **kwargs):
        self.bg_color = bg_color or ModernTheme.ACCENT
        self.text_color = text_color or ModernTheme.BG_PRIMARY
        self.hover_color = hover_color or ModernTheme.ACCENT_HOVER
        self.command = command
        self.width = width
        self.height = height
        
        super().__init__(parent, width=width, height=height, 
                        bg=ModernTheme.BG_PRIMARY, highlightthickness=0, **kwargs)
        
        self.rect = self.create_rectangle(2, 2, width-2, height-2, 
                                         fill=self.bg_color, outline="", 
                                         tags="button")
        self.text_id = self.create_text(width//2, height//2, text=text,
                                       fill=self.text_color, font=(ModernTheme.FONT_FAMILY, 
                                                                   ModernTheme.FONT_SIZE_NORMAL, "bold"))
        
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
    
    def _on_enter(self, event):
        self.itemconfig(self.rect, fill=self.hover_color)
    
    def _on_leave(self, event):
        self.itemconfig(self.rect, fill=self.bg_color)
    
    def _on_click(self, event):
        if self.command:
            self.command()


class V2RayOptimizerModernGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("V2Ray/Xray Optimizer - نسخه مدرن")
        self.root.geometry("1100x750")
        self.root.configure(bg=ModernTheme.BG_PRIMARY)
        
        # Configure RTL support
        self.root.tk.call('tk', 'scaling', 1.2)
        
        self.settings = load_settings()
        self.log_queue = queue.Queue()
        self.progress_var = tk.DoubleVar(value=0)
        self.total_tests = 0
        self.completed_tests = 0
        self.tree_item_map = {}
        
        # Apply modern theme
        self.style = ttk.Style()
        ModernTheme.configure_style(self.style)
        
        self._build_ui()
        self._load_settings_to_ui()
        self.root.after(100, self._process_log_queue)
    
    def _build_ui(self):
        # Header
        header_frame = tk.Frame(self.root, bg=ModernTheme.BG_PRIMARY, height=60)
        header_frame.pack(fill=tk.X, padx=20, pady=(20, 10))
        header_frame.pack_propagate(False)
        
        title_label = tk.Label(header_frame, text="🚀 V2Ray/Xray Optimizer", 
                              font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_TITLE, "bold"),
                              bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.ACCENT)
        title_label.pack(side=tk.RIGHT, padx=10)
        
        subtitle_label = tk.Label(header_frame, text="بهینه‌ساز کانفیگ‌های V2Ray با تست سرعت",
                                  font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                                  bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_SECONDARY)
        subtitle_label.pack(side=tk.RIGHT, padx=10)
        
        # Main notebook
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        # Tabs
        self.sub_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.sub_tab, text="📡 Subscription")
        self._build_sub_tab()
        
        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.settings_tab, text="⚙️ تنظیمات")
        self._build_settings_tab()
        
        self.run_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.run_tab, text="▶️ اجرای تست")
        self._build_run_tab()
        
        self.results_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.results_tab, text="📊 نتایج")
        self._build_results_tab()
    
    def _build_sub_tab(self):
        container = tk.Frame(self.sub_tab, bg=ModernTheme.BG_PRIMARY)
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        # Title
        tk.Label(container, text="لینک‌های Subscription", 
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_LARGE, "bold"),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY).pack(anchor="e", pady=(0, 10))
        
        tk.Label(container, text="هر خط یک URL subscription وارد کنید:",
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_SECONDARY).pack(anchor="e", pady=(0, 5))
        
        # Text area with custom styling
        text_frame = tk.Frame(container, bg=ModernTheme.BG_SECONDARY, bd=1, relief="solid")
        text_frame.pack(fill=tk.BOTH, expand=True, pady=10)
        
        self.sub_text = tk.Text(text_frame, height=12, bg=ModernTheme.BG_SECONDARY,
                               fg=ModernTheme.TEXT_PRIMARY, insertbackground=ModernTheme.ACCENT,
                               font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                               relief="flat", bd=0, wrap=tk.WORD)
        self.sub_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Scrollbar
        scrollbar = ttk.Scrollbar(text_frame, command=self.sub_text.yview)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.sub_text.config(yscrollcommand=scrollbar.set)
        
        # Buttons
        btn_frame = tk.Frame(container, bg=ModernTheme.BG_PRIMARY)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        
        ModernButton(btn_frame, text="📂 بارگذاری از فایل", 
                    command=self._load_sub_from_file, width=140).pack(side=tk.RIGHT, padx=5)
        ModernButton(btn_frame, text="🗑️ پاک‌سازی", 
                    command=lambda: self.sub_text.delete("1.0", tk.END), width=100).pack(side=tk.RIGHT, padx=5)
    
    def _build_settings_tab(self):
        container = tk.Frame(self.settings_tab, bg=ModernTheme.BG_PRIMARY)
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        tk.Label(container, text="تنظیمات تست", 
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_LARGE, "bold"),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY).pack(anchor="e", pady=(0, 20))
        
        # Settings grid
        settings_frame = tk.Frame(container, bg=ModernTheme.BG_PRIMARY)
        settings_frame.pack(fill=tk.BOTH, expand=True)
        
        self.output_name_var = tk.StringVar()
        self.top_n_var = tk.IntVar()
        self.tests_per_cfg_var = tk.IntVar()
        self.max_conc_var = tk.IntVar()
        self.timeout_var = tk.DoubleVar()
        self.xray_path_var = tk.StringVar()
        
        settings = [
            ("نام فایل خروجی:", self.output_name_var, "best-configs"),
            ("تعداد کانفیگ برتر:", self.top_n_var, 10),
            ("تعداد تست برای هر کانفیگ:", self.tests_per_cfg_var, 5),
            ("تعداد همزمانی (Concurrency):", self.max_conc_var, 50),
            ("Timeout هر تست (ثانیه):", self.timeout_var, 3.0),
        ]
        
        for idx, (label, var, default) in enumerate(settings):
            row_frame = tk.Frame(settings_frame, bg=ModernTheme.BG_PRIMARY)
            row_frame.pack(fill=tk.X, pady=8)
            
            tk.Label(row_frame, text=label, font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                    bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY, width=25, anchor="e").pack(side=tk.RIGHT)
            
            entry = tk.Entry(row_frame, textvariable=var, bg=ModernTheme.BG_SECONDARY,
                            fg=ModernTheme.TEXT_PRIMARY, insertbackground=ModernTheme.ACCENT,
                            font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                            relief="flat", bd=1)
            entry.pack(side=tk.RIGHT, padx=10, fill=tk.X, expand=True)
        
        # Xray path
        xray_frame = tk.Frame(settings_frame, bg=ModernTheme.BG_PRIMARY)
        xray_frame.pack(fill=tk.X, pady=8)
        
        tk.Label(xray_frame, text="مسیر باینری xray/v2ray (اختیاری):",
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY, width=25, anchor="e").pack(side=tk.RIGHT)
        
        xray_entry = tk.Entry(xray_frame, textvariable=self.xray_path_var, bg=ModernTheme.BG_SECONDARY,
                             fg=ModernTheme.TEXT_PRIMARY, insertbackground=ModernTheme.ACCENT,
                             font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                             relief="flat", bd=1)
        xray_entry.pack(side=tk.RIGHT, padx=10, fill=tk.X, expand=True)
        
        ModernButton(xray_frame, text="📁 انتخاب", command=lambda: self._browse_file(self.xray_path_var),
                    width=80).pack(side=tk.RIGHT, padx=5)
        
        # Action buttons
        action_frame = tk.Frame(container, bg=ModernTheme.BG_PRIMARY)
        action_frame.pack(fill=tk.X, pady=(20, 0))
        
        ModernButton(action_frame, text="💾 ذخیره تنظیمات", command=self._save_settings,
                    bg_color=ModernTheme.SUCCESS, hover_color="#94e2c9", width=140).pack(side=tk.RIGHT, padx=5)
        ModernButton(action_frame, text="📥 بارگذاری تنظیمات", command=self._load_settings_to_ui,
                    width=140).pack(side=tk.RIGHT, padx=5)
    
    def _build_run_tab(self):
        container = tk.Frame(self.run_tab, bg=ModernTheme.BG_PRIMARY)
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        tk.Label(container, text="اجرای تست", 
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_LARGE, "bold"),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY).pack(anchor="e", pady=(0, 20))
        
        # Start button
        self.start_btn = ModernButton(container, text="🚀 شروع تست و بهینه‌سازی",
                                     command=self._on_start, width=200, height=45,
                                     bg_color=ModernTheme.SUCCESS, hover_color="#94e2c9")
        self.start_btn.pack(pady=10)
        
        # Progress section
        progress_frame = tk.Frame(container, bg=ModernTheme.BG_PRIMARY)
        progress_frame.pack(fill=tk.X, pady=15)
        
        self.status_label = tk.Label(progress_frame, text="وضعیت: آماده",
                                    font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL),
                                    bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_SECONDARY)
        self.status_label.pack(anchor="e", pady=(0, 5))
        
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, 
                                           maximum=100, style="TProgressbar")
        self.progress_bar.pack(fill=tk.X, pady=5)
        
        # Log section
        tk.Label(container, text="📋 لاگ زنده:", 
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_NORMAL, "bold"),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY).pack(anchor="e", pady=(15, 5))
        
        log_frame = tk.Frame(container, bg=ModernTheme.BG_SECONDARY, bd=1, relief="solid")
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        self.log_text = tk.Text(log_frame, height=15, bg=ModernTheme.BG_SECONDARY,
                               fg=ModernTheme.TEXT_PRIMARY, insertbackground=ModernTheme.ACCENT,
                               font=("Consolas", 9), relief="flat", bd=0, state="disabled")
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        log_scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        log_scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scrollbar.set)
    
    def _build_results_tab(self):
        container = tk.Frame(self.results_tab, bg=ModernTheme.BG_PRIMARY)
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)
        
        tk.Label(container, text="نتایج تست", 
                font=(ModernTheme.FONT_FAMILY, ModernTheme.FONT_SIZE_LARGE, "bold"),
                bg=ModernTheme.BG_PRIMARY, fg=ModernTheme.TEXT_PRIMARY).pack(anchor="e", pady=(0, 15))
        
        # Treeview with modern styling
        columns = ("rank", "remark", "protocol", "country", "proxy_latency", "tcp_latency", "success_rate", "copy")
        self.tree = ttk.Treeview(container, columns=columns, show="headings", style="Treeview")
        
        self.tree.heading("rank", text="#")
        self.tree.heading("remark", text="نام کانفیگ")
        self.tree.heading("protocol", text="پروتکل")
        self.tree.heading("country", text="کشور")
        self.tree.heading("proxy_latency", text="Proxy (ms)")
        self.tree.heading("tcp_latency", text="TCP (ms)")
        self.tree.heading("success_rate", text="موفقیت")
        self.tree.heading("copy", text="کپی")
        
        self.tree.column("rank", width=40, anchor="center")
        self.tree.column("remark", width=180, anchor="e")
        self.tree.column("protocol", width=70, anchor="center")
        self.tree.column("country", width=120, anchor="center")
        self.tree.column("proxy_latency", width=80, anchor="center")
        self.tree.column("tcp_latency", width=80, anchor="center")
        self.tree.column("success_rate", width=70, anchor="center")
        self.tree.column("copy", width=70, anchor="center")
        
        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Scrollbars
        tree_scroll_y = ttk.Scrollbar(container, command=self.tree.yview)
        tree_scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.config(yscrollcommand=tree_scroll_y.set)
        
        tree_scroll_x = ttk.Scrollbar(container, command=self.tree.xview, orient="horizontal")
        tree_scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        self.tree.config(xscrollcommand=tree_scroll_x.set)
        
        self.tree.bind("<Button-1>", self._on_tree_click)
        
        # Export buttons
        btn_frame = tk.Frame(container, bg=ModernTheme.BG_PRIMARY)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        
        ModernButton(btn_frame, text="📋 کپی همه", command=self._copy_all_configs,
                    width=120).pack(side=tk.RIGHT, padx=5)
        ModernButton(btn_frame, text="📄 باز کردن فایل خروجی", command=self._open_output_file,
                    width=150).pack(side=tk.RIGHT, padx=5)
    
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
            self._log("فایل با موفقیت بارگذاری شد")
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
        self._log("✓ تنظیمات ذخیره شد")
    
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
        self._log("✓ تنظیمات بارگذاری شد")
    
    def _log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {msg}\n")
    
    def _process_log_queue(self):
        try:
            while not self.log_queue.empty():
                msg = self.log_queue.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert(tk.END, msg)
                self.log_text.see(tk.END)
                self.log_text.config(state="disabled")
        except Exception:
            pass
        self.root.after(100, self._process_log_queue)
    
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
            xray_path = self.xray_path_var.get()
            
            # Check if Xray is available
            if xray_path and os.path.exists(xray_path):
                self._log(f"✓ استفاده از Xray core: {xray_path}")
            else:
                self._log("⚠ Xray core یافت نشد - استفاده از تست TCP")
            
            self.start_btn.config(state="disabled")
            self.completed_tests = 0
            self.total_tests = 0
            self.progress_var.set(0)
            self.status_label.config(text="در حال جمع‌آوری کانفیگ‌ها...")
            
            thread = threading.Thread(
                target=self._worker,
                args=(subs, output_name, top_n, tests_per_config, max_concurrency, timeout, xray_path),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            messagebox.showerror("خطا", str(exc))
    
    def _worker(self, subs, output_name, top_n, tests_per_config, max_concurrency, timeout, xray_path):
        try:
            configs = self._collect_configs(subs)
            if not configs:
                self._log("✗ هیچ کانفیگ معتبری پیدا نشد")
                self._finish()
                return
            
            self.total_tests = len(configs) * tests_per_config
            self._log(f"📊 تعداد کانفیگ: {len(configs)} | کل تست‌ها: {self.total_tests}")
            self._set_status("در حال تست...")
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            progress_cb = lambda: self._increment_progress()
            log_cb = lambda msg: self._log(msg)
            results: List[TestResult] = loop.run_until_complete(
                run_all_tests(configs, tests_per_config, timeout, max_concurrency, progress_cb, log_cb, xray_path)
            )
            loop.close()
            
            ranked = rank_results(results)
            best = ranked[:top_n]
            save_output_files(best, output_name)
            self._update_results(best)
            self._set_status("✓ اتمام تست")
            self._log(f"✓ خروجی ذخیره شد: {output_name}.txt و {output_name}.base64")
        except Exception as exc:
            self._log(f"✗ خطا: {exc}")
            traceback.print_exc()
        finally:
            self._finish()
    
    def _collect_configs(self, subs: List[str]) -> List[ConfigItem]:
        configs: List[ConfigItem] = []
        for url in subs:
            self._log(f"📥 دریافت: {url[:50]}...")
            links = fetch_subscription(url)
            self._log(f"   ✓ {len(links)} کانفیگ یافت شد")
            for link in links:
                cfg = parse_link(link)
                if cfg:
                    configs.append(cfg)
        
        # Deduplicate
        unique = {}
        for cfg in configs:
            unique[cfg.key()] = cfg
        configs = list(unique.values())
        
        # Update countries in background (non-blocking)
        self._update_countries_async(configs)
        
        return configs
    
    def _update_countries_async(self, configs: List[ConfigItem]):
        """Update country info in background thread"""
        def update_task():
            for cfg in configs:
                if not cfg.country:
                    cfg.country = get_country_from_ip(cfg.host)
        
        thread = threading.Thread(target=update_task, daemon=True)
        thread.start()
    
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
            
            filtered = [r for r in best if r.success_count > 0]
            for idx, res in enumerate(filtered, start=1):
                # Show proxy latency if Xray was used, otherwise show TCP latency
                if res.use_xray and res.latencies:
                    proxy_lat = f"{res.avg_latency:.1f}"
                else:
                    proxy_lat = "N/A"
                
                # Show TCP latency
                if res.tcp_latencies:
                    tcp_lat = f"{res.avg_tcp_latency:.1f}"
                else:
                    tcp_lat = "N/A"
                
                item_id = self.tree.insert(
                    "",
                    tk.END,
                    values=(
                        idx,
                        res.config.remark,
                        res.config.protocol,
                        res.config.country,
                        proxy_lat,
                        tcp_lat,
                        f"{res.success_rate:.0f}%",
                        "📋",
                    ),
                )
                self.tree_item_map[item_id] = res.config.raw
        self.root.after(0, update_tree)
    
    def _on_tree_click(self, event):
        col = self.tree.identify_column(event.x)
        if col != "#8":  # Copy column (now 8th column)
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
            self._log("✓ کانفیگ در کلیپ‌بورد کپی شد")
        except Exception as exc:
            self._log(f"✗ خطای کپی: {exc}")
    
    def _copy_all_configs(self):
        links = list(self.tree_item_map.values())
        if not links:
            messagebox.showinfo("اطلاعات", "هیچ کانفیگی برای کپی وجود ندارد")
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(links))
            self._log(f"✓ {len(links)} کانفیگ کپی شد")
        except Exception as exc:
            self._log(f"✗ خطای کپی: {exc}")
    
    def _open_output_file(self):
        output_name = self.output_name_var.get() or DEFAULT_SETTINGS["output_name"]
        if os.path.exists(f"{output_name}.txt"):
            os.startfile(f"{output_name}.txt")
        else:
            messagebox.showinfo("اطلاعات", "فایل خروجی وجود ندارد")
    
    def _finish(self):
        self.root.after(0, lambda: self.start_btn.config(state="normal"))


def main():
    root = tk.Tk()
    app = V2RayOptimizerModernGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
