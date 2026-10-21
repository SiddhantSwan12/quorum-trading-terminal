"""Kronos projections: a K-line foundation model (shiyu-coder/Kronos, MIT, vendored in server/kronos)
run on a symbol's daily bars. It samples paths for the next sessions and keeps the 10th, 50th and
90th percentile close per day. A statistical projection of the price series, nothing more."""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
from datetime import datetime

import qstore

log = logging.getLogger("quorum.kronos")

MODEL_ID = "NeoQuasar/Kronos-base"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
HORIZON = 10     # sessions ahead
PATHS = 30       # sampled paths; the band is their 10th to 90th percentile
LOOKBACK = 400   # bars of context, under the model's 512
LOCK = threading.RLock()  # one load, one projection at a time: one GPU
_STATE = {"predictor": None, "device": None, "error": None, "loadSeconds": None}


def available() -> bool:
    """The packages are importable; the weights load on first use."""
    return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("kronos") is not None


def status() -> dict:
    return {"model": MODEL_ID, "available": available(), "loaded": _STATE["predictor"] is not None,
            "device": _STATE["device"], "error": _STATE["error"]}


def _predictor():
    with LOCK:
        if _STATE["predictor"] is not None:
            return _STATE["predictor"]
        if _STATE["error"]:
            raise RuntimeError(_STATE["error"])
        try:
            t0 = time.time()
            from kronos import Kronos, KronosPredictor, KronosTokenizer
            predictor = KronosPredictor(Kronos.from_pretrained(MODEL_ID), KronosTokenizer.from_pretrained(TOKENIZER_ID), max_context=512)
            _STATE.update(predictor=predictor, device=str(predictor.device), loadSeconds=round(time.time() - t0, 1))
            log.info("Kronos loaded on %s in %.1fs", predictor.device, time.time() - t0)
            return predictor
        except Exception as exc:
            _STATE["error"] = f"Kronos could not load: {type(exc).__name__}: {exc}"
            log.warning(_STATE["error"])
            raise RuntimeError(_STATE["error"]) from exc


def forecast(symbol: str, daily_rows: list, horizon: int = HORIZON, paths: int = PATHS) -> dict:
    """The projection from the last completed bar, kept until a newer bar arrives."""
    if len(daily_rows) < 60:
        raise RuntimeError(f"Kronos needs at least 60 daily bars; {symbol} has {len(daily_rows)}")
    as_of = daily_rows[-1]["date"]
    hit = qstore.get_json("kronos", symbol)
    if hit and hit.get("asOf") == as_of and hit.get("horizon") == horizon and hit.get("paths") == paths and hit.get("model") == MODEL_ID:
        return hit
    import numpy as np
    import pandas as pd

    predictor = _predictor()
    rows = daily_rows[-LOOKBACK:]
    df = pd.DataFrame({"open": [r["o"] for r in rows], "high": [r["h"] for r in rows], "low": [r["l"] for r in rows],
                       "close": [r["c"] for r in rows], "volume": [float(r["v"]) for r in rows]})
    df["amount"] = df["close"] * df["volume"]
    x = pd.Series(pd.to_datetime([r["date"] for r in rows]))
    # The next sessions are taken as weekdays; exchange holidays are not known here.
    y = pd.Series(pd.bdate_range(x.iloc[-1] + pd.Timedelta(days=1), periods=horizon))
    with LOCK:
        t0 = time.time()
        out = predictor.predict_batch(df_list=[df] * paths, x_timestamp_list=[x] * paths, y_timestamp_list=[y] * paths,
                                      pred_len=horizon, T=1.0, top_p=0.9, sample_count=1, verbose=False)
        seconds = round(time.time() - t0, 1)
    closes = np.array([p["close"].values for p in out])
    last = float(rows[-1]["c"])
    p10, p50, p90 = np.percentile(closes, [10, 50, 90], axis=0)
    doc = {
        "model": MODEL_ID, "asOf": as_of, "last": last, "horizon": horizon, "paths": paths,
        "device": _STATE["device"], "seconds": seconds, "computed": datetime.utcnow().isoformat(timespec="seconds"),
        "days": [{"date": y.iloc[i].strftime("%Y-%m-%d"), "p10": round(float(p10[i]), 2), "p50": round(float(p50[i]), 2),
                  "p90": round(float(p90[i]), 2)} for i in range(horizon)],
        "up": round(float((closes[:, -1] > last).mean()), 2),
    }
    qstore.put_json("kronos", symbol, doc)
    log.info("Kronos projection for %s from %s in %.1fs", symbol, as_of, seconds)
    return doc


def brief(symbol: str, doc: dict) -> str:
    """One line of context for the agents."""
    last = doc["last"]
    end = doc["days"][-1]

    def pct(v):
        return f"{(v / last - 1) * 100:+.1f}%"

    return (f"Kronos projection for {symbol} (K-line foundation model {doc['model']}, {doc['paths']} sampled paths from the close "
            f"of {doc['asOf']} at {last}): in {doc['horizon']} sessions the median close is {end['p50']} ({pct(end['p50'])}), the 10th to "
            f"90th percentile {end['p10']} to {end['p90']} ({pct(end['p10'])} to {pct(end['p90'])}), and {round(doc['up'] * 100)}% of "
            f"paths end above the last close. A statistical projection of the price series, not evidence about the business.")
