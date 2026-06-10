#!/usr/bin/env python3
"""Анализ записи tools/lx3/capture.py: какие биты CAN переключаются по пикам-подсказкам.

  python3 tools/lx3/analyze.py lx3_capture_buttons_<ts>.jsonl.gz

Логика: фронт бита (любое направление) считается «попаданием», если случился в окне
WINDOW сек после пика-подсказки своей фазы. Биты, живущие в baseline или часто
переключающиеся вне окон (счётчики, CRC), отсеиваются. Для сценария buttons
дополнительно сверяемся с уже подтверждёнными битами (LFA, CRUISE on/off).

Нумерация бит: byteN/bitM — LSB внутри байта; «глоб. бит» = N*8+M (как старт-бит
little-endian сигналов в opendbc DBC).
"""
import argparse
import bisect
import gzip
import json
import math
from collections import defaultdict

WINDOW = 2.0  # сек после пика, в которые ждём реакцию

# контроль методики (сценарий buttons): живьём подтверждено 2026-06-09
EXPECTED_BUTTONS = {
  "lfa": [(1, 0x10B, 87)],
  "cruise_onoff": [(1, 0x10B, 83), (0, 0x2F0, 53)],
}


def read_lines(path):
  opener = gzip.open if path.endswith(".gz") else open
  with opener(path, "rt") as f:
    for line in f:
      line = line.strip()
      if line:
        yield json.loads(line)


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("capture", help="файл lx3_capture_*.jsonl.gz")
  ap.add_argument("--window", type=float, default=WINDOW)
  ap.add_argument("--top", type=int, default=12, help="кандидатов на фазу в отчёте")
  ap.add_argument("--max-stray", type=int, default=4, help="допустимые фронты вне окон")
  args = ap.parse_args()

  # проход 1: маркеры
  scenario = None
  cues = []            # (t, phase, i) по времени
  phase_cues = {}      # phase -> кол-во пиков
  quiet_spans = []     # [t0, t1] фаз без пиков (baseline)
  spans_open = {}      # phase -> t_start
  t_session = [None, None]
  for rec in read_lines(args.capture):
    if rec[0] == "h":
      scenario = rec[2].get("scenario")
    elif rec[0] == "m":
      t, kind, kw = rec[1], rec[2], rec[3]
      if kind == "session_start":
        t_session[0] = t
      elif kind == "session_end":
        t_session[1] = t
      elif kind == "phase_start":
        spans_open[kw["phase"]] = t
        phase_cues[kw["phase"]] = kw.get("cues", 0)
      elif kind == "phase_end":
        if phase_cues.get(kw["phase"], 0) == 0 and kw["phase"] in spans_open:
          quiet_spans.append((spans_open[kw["phase"]], t))
      elif kind == "cue":
        cues.append((t, kw["phase"], kw["i"]))

  if not cues:
    print("В записи нет пиков-подсказок — сессия не отыграла?")
    return
  cue_times = [c[0] for c in cues]
  if t_session[1] is None:
    t_session[1] = cues[-1][0] + args.window + 2.0

  def classify(t):
    """-> ('hit', phase, i) | 'base' | 'stray' | None (вне сессии)"""
    if t_session[0] is not None and t < t_session[0]:
      return None
    if t > t_session[1]:
      return None
    k = bisect.bisect_right(cue_times, t) - 1
    if k >= 0 and t - cue_times[k] <= args.window:
      return ("hit", cues[k][1], cues[k][2])
    for a, b in quiet_spans:
      if a <= t <= b:
        return "base"
    return "stray"

  # проход 2: фронты бит
  prev = {}
  stats = defaultdict(lambda: {"hits": defaultdict(set), "stray": 0, "base": 0})
  n_frames = 0
  addrs = set()
  for rec in read_lines(args.capture):
    if rec[0] != "c":
      continue
    _, t, src, addr, hexdata = rec
    n_frames += 1
    addrs.add((src, addr))
    data = bytes.fromhex(hexdata)
    key = (src, addr)
    p = prev.get(key)
    prev[key] = data
    if p is None or p == data:
      continue
    cls = classify(t)
    if cls is None:
      continue
    for bi in range(min(len(p), len(data))):
      x = p[bi] ^ data[bi]
      if not x:
        continue
      for bit in range(8):
        if not (x >> bit) & 1:
          continue
        st = stats[(src, addr, bi * 8 + bit)]
        if cls == "base":
          st["base"] += 1
        elif cls == "stray":
          st["stray"] += 1
        else:
          st["hits"][cls[1]].add(cls[2])

  dur = (t_session[1] - (t_session[0] or cue_times[0]))
  print(f"Сценарий: {scenario}; кадров: {n_frames}; адресов: {len(addrs)}; сессия ~{dur:.0f}с")
  print(f"Окно после пика: {args.window}с\n")

  button_phases = [(ph, n) for ph, n in phase_cues.items() if n > 0]
  for ph, ncues in button_phases:
    need = max(2, math.ceil(0.6 * ncues))
    cand = []
    for (src, addr, gbit), st in stats.items():
      nh = len(st["hits"].get(ph, ()))
      if nh >= need and st["base"] == 0 and st["stray"] <= args.max_stray:
        nph = sum(1 for p2, s2 in st["hits"].items() if len(s2) >= 2)
        cand.append((nh, -st["stray"], -nph, src, addr, gbit, st))
    cand.sort(reverse=True)
    print(f"=== фаза {ph} ({ncues} пиков) ===")
    if not cand:
      print("  кандидатов нет (бит не нашёлся — проверь, что нажатия были по пикам)")
    for nh, _, _, src, addr, gbit, st in cand[:args.top]:
      nph = sum(1 for s2 in st["hits"].values() if len(s2) >= 2)
      extra = "" if nph <= 1 else f"  [активен и в {nph - 1} др. фазах — общий/мульти-бит?]"
      print(f"  bus{src} 0x{addr:X} byte{gbit // 8} bit{gbit % 8} (глоб. бит {gbit}): "
            f"{nh}/{ncues} пиков, stray={st['stray']}{extra}")
    print()

  if scenario == "buttons":
    print("=== контроль методики (биты, подтверждённые живьём) ===")
    for ph, exp in EXPECTED_BUTTONS.items():
      ncues = phase_cues.get(ph, 0)
      for src, addr, gbit in exp:
        nh = len(stats.get((src, addr, gbit), {"hits": {}})["hits"].get(ph, ()))
        ok = "PASS" if ncues and nh >= max(2, math.ceil(0.6 * ncues)) else "FAIL"
        print(f"  {ok}: {ph} -> bus{src} 0x{addr:X} бит {gbit}: {nh}/{ncues}")


if __name__ == "__main__":
  main()
