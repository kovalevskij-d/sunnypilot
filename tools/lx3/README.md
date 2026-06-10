# LX3 — сценарии записи CAN со звуковыми подсказками

Цель: однозначно зафиксировать, какая кнопка какие биты дёргает (SET-/RES+/gap/cancel),
плюс раскладка ремня (0x401) и дверей. Устройство пикает — ты нажимаешь — всё пишется
в файл с маркерами времени каждого пика; анализатор на ПК находит коррелирующие биты.

## Деплой на устройство

```bash
ssh comma@<IP>
cd /data/openpilot
git fetch --depth 1 origin lx3-v2 && git checkout FETCH_HEAD -- tools/lx3
```

## Запуск (в машине)

P, зажигание ON, дождаться onroad (pandad публикует "can"). Затем:

```bash
cd /data/openpilot
PYTHONPATH=. /usr/local/venv/bin/python tools/lx3/capture.py --test-beep   # проверка звука
PYTHONPATH=. /usr/local/venv/bin/python tools/lx3/capture.py --scenario buttons
```

Если аудио не открылось (занято soundd): `pkill -f "selfdrive.ui.soundd"` и повторить
(soundd вернётся после полного reboot).

## Звуковая схема

| Звук | Значение |
|------|----------|
| N коротких средних пиков | номер фазы (см. `--list`) |
| 1 длинный низкий | приготовиться, фаза началась |
| короткий ВЫСОКИЙ | выполнить действие СЕЙЧАС (нажать и отпустить) |
| 3 длинных | конец сессии |

Сценарий `buttons` (≈2 мин): 1 baseline (ничего не трогать) → 2 SET- → 3 RES+ →
4 gap → 5 CANCEL → 6 LFA (контроль) → 7 CRUISE on/off (контроль). По 5 пиков на кнопку.
Если промахнулся по пику — просто жди следующий, анализатору хватит 3 из 5.

Другие сценарии: `seatbelt` (чередовать отстегнуть/пристегнуть), `doors`.

## Анализ (на ПК)

```bash
scp comma@<IP>:/data/media/0/lx3_capture_buttons_*.jsonl.gz .
python3 tools/lx3/analyze.py lx3_capture_buttons_<ts>.jsonl.gz
```

Отчёт: по каждой фазе кандидаты `bus/адрес/байт/бит` с числом попаданий в окна пиков.
Для `buttons` в конце контроль методики: LFA (0x10B бит 87) и CRUISE on/off
(0x10B бит 83 + 0x2F0 бит 53) обязаны дать PASS — иначе записи верить нельзя.

После подтверждения битов — правка `CRUISE_BUTTONS_LX3` в
`opendbc/dbc/generator/hyundai/hyundai_canfd.dbc` (сейчас там community-раскладка:
ADAS_BTNS биты 81-82 — НЕ подтверждена) и пересборка DBC.
