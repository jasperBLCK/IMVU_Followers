"""Серверное mp3-радио: разбор фреймов, проверка файлов и непрерывный поток.

Поток работает как Icecast: все слушатели получают один и тот же момент
плейлиста, файлы отдаются с реальной скоростью воспроизведения, при
выключенном эфире идёт тишина, плейлист крутится по кругу.
"""

import base64
import os
import time

import storage

LIVE_DIR = os.path.join(os.path.dirname(__file__), "uploads", "live")

# ~0.13 c тишины (mp3, 128 kbps, 44.1 кГц стерео) — шлётся, когда эфир выключен,
# чтобы плеер не обрывал соединение и звук появлялся сразу после включения
_SILENT_MP3 = base64.b64decode(
    "//uQZAAP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVV//uSZECP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAEVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7kmRAj/AAAGkAAAAIAAANIAAA"
    "AQAAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/"
    "+5JkQI/wAABpAAAACAAADSAAAAEAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVTEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVV//uSZECP8AAAaQAAAAgAAA0gAAABAAABpAAAACAAADSAAAAEVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVQ=="
)
_SILENT_SECS = 0.13

# битрейты (кбит/с) для MPEG1 / MPEG2(2.5) Layer III
_MP3_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MP3_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3_frame_len(data, i):
    """Длина валидного mp3-фрейма (Layer III) с позиции i, иначе 0."""
    if i + 4 > len(data) or data[i] != 0xFF or (data[i + 1] & 0xE0) != 0xE0:
        return 0
    version = (data[i + 1] >> 3) & 0x03   # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer = (data[i + 1] >> 1) & 0x03     # 1 = Layer III
    if version == 1 or layer != 1:
        return 0
    br_idx = (data[i + 2] >> 4) & 0x0F
    sr_idx = (data[i + 2] >> 2) & 0x03
    if br_idx in (0, 15) or sr_idx == 3:
        return 0
    bitrate = (_MP3_BITRATES_V1 if version == 3 else _MP3_BITRATES_V2)[br_idx] * 1000
    rate = _MP3_RATES[version][sr_idx]
    padding = (data[i + 2] >> 1) & 0x01
    samples = 1152 if version == 3 else 576
    return samples * bitrate // (8 * rate) + padding


def _mp3_frame_align(data):
    """Сдвиг до начала настоящего mp3-фрейма: заголовок валиден и за фреймом
    следует ещё один валидный заголовок (защита от ложного sync в аудиоданных)."""
    for i in range(len(data) - 4):
        ln = _mp3_frame_len(data, i)
        if not ln:
            continue
        j = i + ln
        if j + 4 > len(data) or _mp3_frame_len(data, j):
            return data[i:]
    return data


def _id3_size(data):
    if data[:3] != b"ID3" or len(data) < 10:
        return 0
    return (
        (data[6] & 0x7F) << 21
        | (data[7] & 0x7F) << 14
        | (data[8] & 0x7F) << 7
        | (data[9] & 0x7F)
    ) + 10


def is_real_mp3(fh):
    """Проверка содержимого: это настоящий mp3, а не переименованный m4a/webm.
    Пропускает ID3-тег и ищет два подряд валидных заголовка фрейма."""
    head = fh.read(10)
    skip = _id3_size(head)
    if skip:
        fh.seek(skip)
        data = fh.read(65536)
    else:
        data = head + fh.read(65536)
    fh.seek(0)
    for i in range(len(data) - 4):
        ln = _mp3_frame_len(data, i)
        if ln and (i + ln + 4 > len(data) or _mp3_frame_len(data, i + ln)):
            return True
    return False


def mp3_duration(path):
    """Длительность mp3 в секундах: проход по фреймам (работает и для VBR)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0.0
    i = _id3_size(data)
    seconds = 0.0
    n = len(data)
    while i < n - 4:
        ln = _mp3_frame_len(data, i)
        if not ln:
            i += 1  # ресинк после мусора между фреймами
            continue
        version = (data[i + 1] >> 3) & 0x03
        sr_idx = (data[i + 2] >> 2) & 0x03
        rate = _MP3_RATES[version][sr_idx]
        seconds += (1152 if version == 3 else 576) / rate
        i += ln
    return seconds


def live_now(meta):
    """Что играет прямо сейчас: индекс трека и смещение в секундах."""
    tracks = [t for t in meta["tracks"] if t.get("duration", 0) > 0]
    total = sum(t["duration"] for t in tracks)
    if not meta["on"] or not tracks or total <= 0:
        return None
    pos = (time.time() - meta["started_at"]) % total
    for i, t in enumerate(tracks):
        if pos < t["duration"]:
            return {"index": i, "offset": round(pos, 3)}
        pos -= t["duration"]
    return {"index": 0, "offset": 0.0}


def stream_generator():
    """Бесконечный mp3-поток: отдаёт треки плейлиста с текущего места эфира,
    как Icecast-радио — все слушатели получают один и тот же момент."""
    chunk_size = 16384
    buffer_ahead = 10.0   # секунд аудио вперёд в устоявшемся режиме
    initial_burst = 20.0  # стартовый запас при подключении, чтобы плеер не лагал
    lead = initial_burst
    while True:
        meta = storage.load_live()
        now = live_now(meta)
        if now is None:
            # эфир выключен или пуст — держим слушателя тишиной
            yield _SILENT_MP3
            time.sleep(_SILENT_SECS)
            continue
        tracks = [t for t in meta["tracks"] if t.get("duration", 0) > 0]
        track = tracks[now["index"]]
        path = os.path.join(LIVE_DIR, track["file"])
        try:
            size = os.path.getsize(path)
        except OSError:
            time.sleep(1.0)
            continue
        bps = size / track["duration"]  # байт в секунду (усреднённый битрейт)
        start = int(now["offset"] * bps)
        t0 = time.time()
        # момент, когда этот трек закончится в эфире
        track_ends_at = t0 + max(track["duration"] - now["offset"], 0.0)
        sent = 0
        try:
            with open(path, "rb") as fh:
                fh.seek(min(start, max(size - 1, 0)))
                first = True
                while True:
                    data = fh.read(chunk_size)
                    if not data:
                        break
                    if first:
                        data = _mp3_frame_align(data)
                        first = False
                    yield data
                    sent += len(data)
                    ahead = sent / bps - (time.time() - t0) - lead
                    if ahead > 0:
                        lead = buffer_ahead  # стартовый запас отдан
                        time.sleep(min(ahead, 1.0))
        except OSError:
            time.sleep(1.0)
            continue
        # файл отдан раньше конца трека (буфер) — ждём, пока эфир перейдёт
        # на следующий трек (небольшой запас против зацикливания на стыке)
        time.sleep(max(track_ends_at - time.time() + 0.05, 0.05))
