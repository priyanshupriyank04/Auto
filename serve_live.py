import csv
import json
import os
import re
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

try:
	from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:  # pragma: no cover
	from backports.zoneinfo import ZoneInfo  # type: ignore


IST = ZoneInfo("Asia/Kolkata")


def get_logs_dir() -> Path:
	"""
	Resolve the logs directory from LOGS_DIR env var or default to ./logs relative to CWD.
	"""
	env_dir = os.environ.get("LOGS_DIR")
	if env_dir:
		return Path(env_dir).expanduser().resolve()
	return (Path.cwd() / "logs").resolve()


def today_ist() -> date:
	return datetime.now(tz=IST).date()


def parse_iso_ts(value: str) -> datetime:
	"""
	Parse an ISO timestamp. The trade logs provide `timestamp_ist` as ISO string.
	"""
	return datetime.fromisoformat(value.replace("Z", "+00:00"))


def read_json_with_retry(file_path: Path) -> Any:
	"""
	Read JSON with a single retry on JSONDecodeError (e.g., mid-write).
	"""
	try:
		with file_path.open("r", encoding="utf-8") as f:
			return json.load(f)
	except json.JSONDecodeError:
		time.sleep(0.1)  # 100ms
		with file_path.open("r", encoding="utf-8") as f:
			return json.load(f)


def parse_heartbeat_line(line: str) -> Dict[str, Any]:
	"""
	Parse a heartbeat line like:
	[15:33:38] BTC: $66542.00 | Range: [66566-66660] | Status: SESSION_ENDED | Armed: waiting | Side: no | SLs: 1/3 | TP: None | Halted: No | Eq: $0.00
	"""
	text = line.strip()

	def find(pattern: str) -> Optional[re.Match]:
		return re.search(pattern, text)

	def to_float(num_str: str) -> float:
		return float(num_str.replace(",", ""))

	time_m = find(r"^\[(?P<t>\d{2}:\d{2}:\d{2})\]")
	price_m = find(r"BTC:\s*\$(?P<p>[0-9,]+(?:\.\d+)?)")
	range_m = find(r"Range:\s*\[(?P<lo>[0-9,]+(?:\.\d+)?)-(?P<hi>[0-9,]+(?:\.\d+)?)\]")
	status_m = find(r"Status:\s*(?P<s>[A-Za-z_]+)")
	armed_m = find(r"Armed:\s*(?P<a>[A-Za-z_]+)")
	side_m = find(r"Side:\s*(?P<side>[A-Za-z_]+)")
	sls_m = find(r"SLs:\s*(?P<sls>\d+/\d+)")
	tp_m = find(r"TP:\s*(?P<tp>[^|\s]+)")
	halted_m = find(r"Halted:\s*(?P<h>(?:Yes|No))")
	eq_m = find(r"Eq:\s*\$(?P<eq>[0-9,]+(?:\.\d+)?)")

	if not all([time_m, price_m, range_m, status_m, armed_m, side_m, sls_m, tp_m, halted_m, eq_m]):
		raise ValueError("heartbeat format mismatch")

	tp_raw = tp_m.group("tp")
	if tp_raw.lower() == "none":
		take_profit: Optional[float] = None
	else:
		try:
			take_profit = float(tp_raw.lstrip("$").replace(",", ""))
		except ValueError:
			take_profit = None

	return {
		"btc_price": to_float(price_m.group("p")),
		"range_low": to_float(range_m.group("lo")),
		"range_high": to_float(range_m.group("hi")),
		"status": status_m.group("s"),
		"armed": armed_m.group("a"),
		"side": side_m.group("side"),
		"stop_losses": sls_m.group("sls"),
		"take_profit": take_profit,
		"halted": True if halted_m.group("h") == "Yes" else False,
		"equity": to_float(eq_m.group("eq")),
		"last_updated": time_m.group("t"),
	}


def iter_trade_files_between(trades_dir: Path, from_date: Optional[date], to_date: Optional[date]) -> List[Path]:
	files = []
	if not trades_dir.exists():
		return files
	for p in sorted(trades_dir.glob("*_trades.json")):
		# Extract YYYY-MM-DD
		try:
			day = date.fromisoformat(p.name.split("_trades.json")[0])
		except Exception:
			continue
		if from_date and day < from_date:
			continue
		if to_date and day > to_date:
			continue
		files.append(p)
	return files


def iter_pnl_files_between(logs_dir: Path, from_date: Optional[date], to_date: Optional[date]) -> List[Path]:
	files = []
	if not logs_dir.exists():
		return files
	for p in sorted(logs_dir.glob("pnl_*.csv")):
		try:
			# filename pnl_YYYY-MM-DD.csv
			stem = p.stem  # pnl_YYYY-MM-DD
			day_str = stem.split("pnl_")[1]
			day = date.fromisoformat(day_str)
		except Exception:
			continue
		if from_date and day < from_date:
			continue
		if to_date and day > to_date:
			continue
		files.append(p)
	return files


app = FastAPI()

app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)


@app.get("/health")
def health() -> Dict[str, str]:
	return {"status": "ok"}


@app.get("/heartbeat")
def heartbeat() -> Dict[str, Any]:
	logs_dir = get_logs_dir()
	hb_path = logs_dir / "heartbeat.txt"
	if not hb_path.exists():
		# HTTP 503 with error payload
		raise HTTPException(status_code=503, detail={"error": "file not found", "file": "heartbeat.txt"})
	try:
		line = hb_path.read_text(encoding="utf-8").strip()
		data = parse_heartbeat_line(line)
		return data
	except ValueError:
		raise HTTPException(status_code=500, detail={"error": "failed to parse heartbeat", "file": "heartbeat.txt"})


@app.get("/trades")
def trades(
	date_str: Optional[str] = Query(default=None, alias="date"),
	since: Optional[str] = Query(default=None),
) -> List[Dict[str, Any]]:
	"""
	Read logs/trades/{date}_trades.json, using IST 'today' by default.
	Optional `since` filters events with timestamp_ist strictly after the given ISO timestamp.
	"""
	day = date.fromisoformat(date_str) if date_str else today_ist()
	logs_dir = get_logs_dir()
	trade_file = logs_dir / "trades" / f"{day.isoformat()}_trades.json"
	if not trade_file.exists():
		return []

	try:
		data = read_json_with_retry(trade_file)
	except json.JSONDecodeError:
		raise HTTPException(status_code=500, detail={"error": "failed to parse trades file", "file": trade_file.name})

	if not isinstance(data, list):
		return []

	if since:
		try:
			since_dt = parse_iso_ts(since)
		except Exception:
			since_dt = None
		if since_dt:
			filtered = []
			for item in data:
				try:
					ts = parse_iso_ts(str(item.get("timestamp_ist")))
					if ts > since_dt:
						filtered.append(item)
				except Exception:
					continue
			return filtered
	return data


@app.get("/trades/history")
def trades_history(
	from_str: Optional[str] = Query(default=None, alias="from"),
	to_str: Optional[str] = Query(default=None, alias="to"),
) -> List[Dict[str, Any]]:
	logs_dir = get_logs_dir()
	trades_dir = logs_dir / "trades"
	from_date = date.fromisoformat(from_str) if from_str else None
	to_date = date.fromisoformat(to_str) if to_str else None

	all_events: List[Dict[str, Any]] = []
	for fpath in iter_trade_files_between(trades_dir, from_date, to_date):
		try:
			events = read_json_with_retry(fpath)
			if not isinstance(events, list):
				continue
			for ev in events:
				kind = ev.get("event")
				if kind in ("trade_entry", "trade_exit"):
					all_events.append(ev)
		except json.JSONDecodeError:
			# Skip files that fail to parse
			continue
		except OSError:
			continue

	def sort_key(ev: Dict[str, Any]) -> Tuple[float, str]:
		ts_raw = str(ev.get("timestamp_ist"))
		try:
			return (parse_iso_ts(ts_raw).timestamp(), ts_raw)
		except Exception:
			return (0.0, ts_raw)

	all_events.sort(key=sort_key)
	return all_events


@app.get("/pnl")
def pnl(
	date_str: Optional[str] = Query(default=None, alias="date"),
) -> List[Dict[str, Any]]:
	day = date.fromisoformat(date_str) if date_str else today_ist()
	logs_dir = get_logs_dir()
	pnl_file = logs_dir / f"pnl_{day.isoformat()}.csv"
	if not pnl_file.exists():
		return []

	rows: List[Dict[str, Any]] = []
	try:
		with pnl_file.open("r", encoding="utf-8", newline="") as f:
			reader = csv.DictReader(f)
			for row in reader:
				# Return as-is (strings); frontend can convert types if needed
				rows.append(dict(row))
	except Exception:
		raise HTTPException(status_code=500, detail={"error": "failed to parse pnl file", "file": pnl_file.name})
	return rows


@app.get("/pnl/history")
def pnl_history(
	from_str: Optional[str] = Query(default=None, alias="from"),
	to_str: Optional[str] = Query(default=None, alias="to"),
) -> List[Dict[str, Any]]:
	logs_dir = get_logs_dir()
	from_date = date.fromisoformat(from_str) if from_str else None
	to_date = date.fromisoformat(to_str) if to_str else None

	rows: List[Dict[str, Any]] = []
	for fpath in iter_pnl_files_between(logs_dir, from_date, to_date):
		try:
			with fpath.open("r", encoding="utf-8", newline="") as f:
				reader = csv.DictReader(f)
				for row in reader:
					rows.append(dict(row))
		except Exception:
			# Skip files that fail to parse
			continue

	def sort_key(r: Dict[str, Any]) -> Tuple[str, str]:
		return (str(r.get("date", "")), str(r.get("time_ist", "")))

	rows.sort(key=sort_key)
	return rows

