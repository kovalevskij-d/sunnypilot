#!/usr/bin/env python3
"""LX3 (Palisade Hybrid 2026) — запись CAN-сценариев со звуковыми подсказками.

Запускать НА УСТРОЙСТВЕ (comma 4), в машине: P, зажигание ON, openpilot onroad
(pandad публикует сокет "can").

  cd /data/openpilot
  PYTHONPATH=. /usr/local/venv/bin/python tools/lx3/capture.py --scenario buttons

Звуковая схема:
  N коротких СРЕДНИХ пиков (660 Гц) — номер фазы (1..N, см. --list)
  1 длинный НИЗКИЙ пик (440 Гц)     — приготовиться, фаза началась
  короткий ВЫСОКИЙ пик (1320 Гц)    — выполнить действие СЕЙЧАС (нажать и отпустить)
  3 длинных пика                    — конец сессии

Если звук не открылся (устройство занято soundd):
  pkill -f "selfdrive.ui.soundd"   # вернётся сам после полного reboot

Выход: /data/media/0/lx3_capture_<scenario>_<ts>.jsonl.gz
Анализ на ПК: tools/lx3/analyze.py <файл>
"""
import argparse
import gzip
import json
import sys
import threading
import time
from collections import deque

SAMPLE_RATE = 48000
DEFAULT_PERIOD = 2.5  # сек между пиками-действиями
OUT_DIR = "/data/media/0"

PHASE_FREQ, READY_FREQ, CUE_FREQ = 660, 440, 1320

# Фаза: name, action (что делать по пику), cues (0 = тихая фаза длиной dur),
# period (перерыв между пиками, по умолчанию DEFAULT_PERIOD)
SCENARIOS = {
  "buttons": {
    "desc": "Кнопки руля: SET-/RES+/gap/cancel + контрольные LFA и CRUISE on/off",
    "phases": [
      {"name": "baseline", "action": "ничего не нажимать", "cues": 0, "dur": 12.0},
      {"name": "set_minus", "action": "SET- (качелька круиза ВНИЗ)", "cues": 5},
      {"name": "res_plus", "action": "RES+ (качелька круиза ВВЕРХ)", "cues": 5},
      {"name": "gap_dist", "action": "кнопка дистанции (gap)", "cues": 5},
      {"name": "cancel", "action": "CANCEL / pause", "cues": 5},
      {"name": "lfa", "action": "кнопка LFA (контроль методики)", "cues": 5},
      {"name": "cruise_onoff", "action": "кнопка CRUISE вкл/выкл (контроль)", "cues": 5},
    ],
  },
  "seatbelt": {
    "desc": "Ремень водителя: раскладка 0x401 (по пику чередовать отстегнуть/пристегнуть)",
    "phases": [
      {"name": "baseline", "action": "сидеть пристёгнутым, ничего не делать", "cues": 0, "dur": 10.0},
      {"name": "belt_toggle", "action": "по пику ЧЕРЕДОВАТЬ: отстегнуть / пристегнуть", "cues": 6, "period": 5.0},
    ],
  },
  "doors": {
    "desc": "Дверь водителя: по пику чередовать открыть/закрыть",
    "phases": [
      {"name": "baseline", "action": "дверь закрыта, ничего не делать", "cues": 0, "dur": 10.0},
      {"name": "door_toggle", "action": "по пику ЧЕРЕДОВАТЬ: открыть / закрыть дверь", "cues": 6, "period": 6.0},
    ],
  },
}


def boottime() -> float:
  # та же шкала, что logMonoTime у сообщений cereal
  return time.clock_gettime(time.CLOCK_BOOTTIME)


class Beeper:
  def __init__(self):
    import numpy as np
    import sounddevice as sd
    self.np = np
    self.stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    self.stream.start()

  def tone(self, freq: float, dur: float, vol: float = 0.9):
    np = self.np
    n = int(SAMPLE_RATE * dur)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    fade = np.minimum(1.0, np.minimum(t, dur - t) / 0.01)  # 10мс фейд против щелчков
    self.stream.write((vol * fade * np.sin(2 * np.pi * freq * t)).astype(np.float32).reshape(-1, 1))

  def close(self):
    try:
      self.stream.stop()
      self.stream.close()
    except Exception:
      pass


def run_session(scenario: dict, beep: Beeper, mark, stop: threading.Event):
  mark("session_start", scenario=scenario["name"])
  if stop.wait(1.0):
    return
  for num, ph in enumerate(scenario["phases"], start=1):
    cues = ph.get("cues", 0)
    period = ph.get("period", DEFAULT_PERIOD)
    print(f"[фаза {num}/{len(scenario['phases'])}] {ph['name']}: {ph['action']}", flush=True)
    for _ in range(num):  # номер фазы пиками
      beep.tone(PHASE_FREQ, 0.12)
      if stop.wait(0.22):
        return
    if stop.wait(0.9):
      return
    beep.tone(READY_FREQ, 0.5)  # приготовиться
    mark("phase_start", phase=ph["name"], num=num, action=ph["action"], cues=cues)
    if cues == 0:
      if stop.wait(ph.get("dur", 10.0)):
        return
    else:
      if stop.wait(1.3):
        return
      for i in range(cues):
        mark("cue", phase=ph["name"], i=i)
        beep.tone(CUE_FREQ, 0.18)
        if stop.wait(period - 0.18):
          return
    mark("phase_end", phase=ph["name"])
    if stop.wait(1.5):
      return
  mark("session_end")
  for _ in range(3):
    beep.tone(READY_FREQ, 0.5)
    if stop.wait(0.35):
      return


def test_beep():
  beep = Beeper()
  print("3 коротких (номер фазы) — длинный (приготовиться) — 2 высоких (действие)")
  for _ in range(3):
    beep.tone(PHASE_FREQ, 0.12)
    time.sleep(0.22)
  time.sleep(0.7)
  beep.tone(READY_FREQ, 0.5)
  time.sleep(1.0)
  for _ in range(2):
    beep.tone(CUE_FREQ, 0.18)
    time.sleep(1.0)
  beep.close()


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--scenario", default="buttons", choices=sorted(SCENARIOS.keys()))
  ap.add_argument("--list", action="store_true", help="показать сценарии и фазы")
  ap.add_argument("--test-beep", action="store_true", help="проверить звук и выйти")
  ap.add_argument("--out", default=OUT_DIR, help="каталог для записи")
  ap.add_argument("--all-buses", action="store_true", help="писать и bus2 (камера), по умолчанию только 0 и 1")
  args = ap.parse_args()

  if args.list:
    for name, sc in SCENARIOS.items():
      print(f"{name}: {sc['desc']}")
      for num, ph in enumerate(sc["phases"], start=1):
        n = f"{ph.get('cues', 0)} пиков" if ph.get("cues", 0) else f"тишина {ph.get('dur', 10.0):.0f}с"
        print(f"  фаза {num} [{ph['name']}] {n}: {ph['action']}")
    return

  try:
    beep = Beeper()
  except Exception as e:
    print(f"Не удалось открыть аудио: {e}")
    print('Похоже, выход занят soundd. Сделай: pkill -f "selfdrive.ui.soundd" и повтори.')
    sys.exit(2)

  if args.test_beep:
    beep.close()
    test_beep()
    return

  import cereal.messaging as messaging  # доступно только на устройстве/в среде openpilot

  scenario = dict(SCENARIOS[args.scenario], name=args.scenario)
  buses = {0, 1, 2} if args.all_buses else {0, 1}
  path = f"{args.out}/lx3_capture_{args.scenario}_{int(time.time())}.jsonl.gz"

  markers: deque = deque()

  def mark(kind, **kw):
    markers.append(["m", round(boottime(), 4), kind, kw])

  sock = messaging.sub_sock("can", conflate=False, timeout=100)
  stop = threading.Event()
  th = threading.Thread(target=run_session, args=(scenario, beep, mark, stop), daemon=True)

  frames = 0
  t_start = time.monotonic()
  warned = False
  f = gzip.open(path, "wt", compresslevel=1)
  f.write(json.dumps(["h", round(boottime(), 4),
                      {"scenario": args.scenario, "buses": sorted(buses),
                       "phases": scenario["phases"]}], separators=(",", ":")) + "\n")
  print(f"Пишу в {path} (шины {sorted(buses)}). Ctrl+C — прервать.", flush=True)
  th.start()
  try:
    while th.is_alive() or markers:
      while markers:
        f.write(json.dumps(markers.popleft(), separators=(",", ":")) + "\n")
      for evt in messaging.drain_sock(sock, wait_for_one=True):
        t = evt.logMonoTime * 1e-9
        for c in evt.can:
          if c.src in buses:
            f.write(json.dumps(["c", round(t, 4), c.src, c.address, bytes(c.dat).hex()],
                               separators=(",", ":")) + "\n")
            frames += 1
      if frames == 0 and not warned and time.monotonic() - t_start > 5.0:
        warned = True
        print("ВНИМАНИЕ: CAN-кадров нет. Устройство onroad? pandad работает?", flush=True)
  except KeyboardInterrupt:
    print("\nПрервано — файл сохраняю.", flush=True)
  finally:
    stop.set()
    th.join(timeout=3.0)
    beep.close()
    while markers:
      f.write(json.dumps(markers.popleft(), separators=(",", ":")) + "\n")
    f.close()
  print(f"Готово: {path} ({frames} кадров)")


if __name__ == "__main__":
  main()
