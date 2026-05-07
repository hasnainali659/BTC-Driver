"""
Storage layer — SQLite persistence for signals + forecasts.

Lets you:
  - Backtest your forecast accuracy over time
  - Identify which signals predict outcomes vs which are noise
  - Provide a queryable history to downstream consumers
"""
import json
import logging
import sqlite3
import numpy as np
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
from typing import List, Optional

from signals import Signal, Forecast
from config import CONFIG

logger = logging.getLogger(__name__)


class NumpyJSONEncoder(json.JSONEncoder):
    """Handle numpy types in serialization."""
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def _dumps(obj) -> str:
    return json.dumps(obj, cls=NumpyJSONEncoder)


def _connect() -> sqlite3.Connection:
    Path(CONFIG.LOG_DIR).mkdir(parents=True, exist_ok=True)
    db_path = Path(CONFIG.LOG_DIR) / CONFIG.DB_PATH
    return sqlite3.connect(str(db_path))


def init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forecast_id INTEGER,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                raw_value TEXT,
                score REAL,
                confidence TEXT,
                meta_json TEXT,
                FOREIGN KEY (forecast_id) REFERENCES forecasts(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS forecasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                horizon_hours INTEGER,
                direction TEXT,
                composite REAL,
                confidence TEXT,
                agreement_pct REAL,
                signal_count INTEGER,
                btc_price REAL,
                summary TEXT,
                key_drivers_json TEXT,
                contradictions_json TEXT,
                -- For accuracy backtesting (filled later by backtester)
                actual_btc_price_at_horizon REAL,
                actual_change_pct REAL,
                forecast_correct INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_forecasts_time ON forecasts(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_name ON signals(name, timestamp)")
        conn.commit()
    finally:
        conn.close()


def save_forecast(forecast: Forecast, signals: List[Signal]) -> int:
    """Persist forecast and its signals. Returns forecast id."""
    init_db()
    conn = _connect()
    try:
        cursor = conn.execute("""
            INSERT INTO forecasts (
                timestamp, horizon_hours, direction, composite, confidence,
                agreement_pct, signal_count, btc_price, summary,
                key_drivers_json, contradictions_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            forecast.timestamp.isoformat(),
            forecast.horizon_hours,
            forecast.direction.value,
            forecast.composite,
            forecast.confidence.value,
            forecast.agreement_pct,
            forecast.signal_count,
            forecast.btc_price,
            forecast.summary,
            _dumps(forecast.key_drivers),
            _dumps(forecast.contradictions),
        ))
        forecast_id = cursor.lastrowid

        for s in signals:
            conn.execute("""
                INSERT INTO signals (
                    forecast_id, timestamp, source, category, name,
                    raw_value, score, confidence, meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                forecast_id,
                s.timestamp.isoformat(),
                s.source,
                s.category,
                s.name,
                _dumps(s.raw_value),
                s.score,
                s.confidence.value,
                _dumps(s.meta),
            ))

        conn.commit()
        return forecast_id
    finally:
        conn.close()


def latest_forecast() -> Optional[dict]:
    """Get the most recent forecast as a dict (for downstream consumers)."""
    init_db()
    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("""
            SELECT * FROM forecasts ORDER BY timestamp DESC LIMIT 1
        """).fetchone()
        if not row:
            return None
        d = dict(row)
        d['key_drivers'] = json.loads(d.pop('key_drivers_json') or '[]')
        d['contradictions'] = json.loads(d.pop('contradictions_json') or '[]')
        return d
    finally:
        conn.close()


def export_latest_forecast_to_json(filepath: str = "latest_forecast.json"):
    """Write the latest forecast to a JSON file for external systems to consume."""
    fc = latest_forecast()
    if not fc:
        return False
    out_path = Path(CONFIG.LOG_DIR) / filepath
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(fc, f, indent=2, default=str)
    return True
