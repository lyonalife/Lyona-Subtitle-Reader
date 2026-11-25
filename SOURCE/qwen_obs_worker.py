# -*- coding: utf-8 -*-
"""Isolated native Qwen TTS process: persistent model, one line of speech at a time.

No samples or synthesized audio are uploaded or saved.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from ru_pronunciation import prepare_text
from qwen_profiles import PROFILES, LOW, voice_profile
from qwen_audio import LoudnessNormalizer, MAX_GAIN

PREFIX = 'LYONA_EVENT::'


def emit(kind, **kwargs):
    # ASCII JSON also survives a Windows console using a non-UTF-8 code page.
    print(PREFIX + json.dumps({'type': kind, **kwargs}, ensure_ascii=True), flush=True)


def resolve_output_device(device: str):
    try:
        import pyaudio
        audio = pyaudio.PyAudio()
        try:
            if not device or device == 'По умолчанию':
                info = audio.get_default_output_device_info()
                emit('status', message='Вывод по умолчанию: ' + str(info['name']))
                return int(info['index'])
            choices = []
            for i in range(audio.get_device_count()):
                info = audio.get_device_info_by_index(i)
                if int(info.get('maxOutputChannels', 0)) > 0:
                    host = audio.get_host_api_info_by_index(info['hostApi'])['name']
                    choices.append((i, str(info['name']), str(host)))
        finally:
            audio.terminate()
        normalize = lambda s: ''.join(ch for ch in s.casefold() if ch.isalnum())
        wanted = normalize(device)
        # SDL shows the full Windows name; PortAudio's MME backend may shorten
        # it. DirectSound can accept writes without delivering audible samples
        # on some virtual devices, so prefer an unambiguous matching MME route.
        exact = [item for item in choices if normalize(item[1]) == wanted]
        mme = [item for item in choices if item[2] == 'MME' and
               len(normalize(item[1])) >= 18 and wanted.startswith(normalize(item[1]))]
        if len(mme) == 1:
            matches = mme
        elif exact:
            matches = sorted(exact, key=lambda item: 0 if item[2] == 'Windows WASAPI' else 1)
        else:
            matches = [item for item in choices if len(normalize(item[1])) >= 18 and
                       wanted.startswith(normalize(item[1]))]
        if len({normalize(name) for _, name, _host in matches}) > 1:
            raise RuntimeError('Неоднозначное аудиоустройство: ' + device + '. Выбери полное название.')
        if not matches:
            listing = ', '.join(n for _, n, _host in choices[:12])
            raise RuntimeError('Аудиоустройство Qwen не найдено: ' + device + '. Доступны: ' + listing)
        idx, name, host = matches[0]
        emit('status', message=f'Вывод через {name} ({host}, PortAudio #{idx})')
        return idx
    except ImportError as exc:
        raise RuntimeError('В среде Qwen не найден PyAudio.') from exc



def play_adjusted(stream, tempo: float, gain: float, normalize: bool, audio_idx, on_generated, on_first):
    """Apply streaming gain and normalization after optional pitch-preserving tempo."""
    import pyaudio
    from qwen_tempo import LiveTempo

    pa = pyaudio.PyAudio()
    output = None
    shifter = None
    levels = None
    started = False
    generated = False
    written_bytes = 0
    sample_rate = 24000

    try:
        def accept_pcm(chunk):
            nonlocal output, shifter, levels, started, generated, written_bytes, sample_rate
            if not chunk:
                return
            if not generated:
                generated = True
            on_generated()
            if output is None:
                # Stream player can use a different output rate than Qwen's 24 kHz.
                player_audio = stream.player.audio_stream
                sample_rate = int(player_audio.actual_sample_rate or player_audio.config.rate)
                if not (8000 <= sample_rate <= 192000):
                    raise RuntimeError('Unsupported Qwen audio rate: ' + str(sample_rate))
                levels = LoudnessNormalizer(sample_rate, gain, normalize)
                if tempo > 1.001:
                    shifter = LiveTempo(sample_rate, tempo)
                output = pa.open(format=pyaudio.paInt16, channels=1,
                                 rate=sample_rate, output=True,
                                 output_device_index=audio_idx,
                                 frames_per_buffer=512)
            ready = shifter.feed(chunk) if shifter is not None else chunk
            if ready:
                output.write(levels.process(ready))
                written_bytes += len(ready)
                if not started:
                    started = True
                    on_first()

        stream.play(muted=True, on_audio_chunk=accept_pcm)
        if shifter is not None and output is not None:
            tail = shifter.finish()
            if tail:
                output.write(levels.process(tail))
                written_bytes += len(tail)
                if not started:
                    started = True
                    on_first()
        return written_bytes / (2 * sample_rate)
    finally:
        try:
            if output is not None:
                output.stop_stream()
                output.close()
        finally:
            pa.terminate()


def main():
    startup_started = time.perf_counter()
    from qwen_compute import select_backend, cpu_factory
    compute, reason = select_backend()
    emit('backend', backend=compute, reason=reason)
    if compute == 'CUDA':
        from qwen_native_startup import configure
        configure()
        import os
        emit('status', message='CUDA: KV context=' + os.environ.get('LYONA_QWEN_CONTEXT', '4096')
             + '; F32; CUDA Graphs=' + ('off' if 'GGML_CUDA_DISABLE_GRAPHS' in os.environ else 'on'))
    voice_dir = Path(sys.argv[1]).resolve()
    device = sys.argv[2] if len(sys.argv) > 2 else 'По умолчанию'
    model = sys.argv[3] if len(sys.argv) > 3 else LOW
    profile = voice_profile(str(voice_dir), model)
    sample = profile['sample']
    if not sample.is_file():
        raise RuntimeError('Не найден образец голоса: ' + str(sample))
    from RealtimeTTS import QwenEngine, QwenVoice, TextToAudioStream
    audio_idx = resolve_output_device(device)
    voice = QwenVoice(name=profile['name'], ref_audio=str(sample),
                      ref_text=profile.get('ref_text'), language='russian')
    try:
        emit('status', message=f'Инициализация {compute}: модель, ICL-референс и прогрев. '
             'При первом запуске CUDA возможна длительная JIT-компиляция.')
        engine = QwenEngine(
            voice=voice, max_new_tokens=140, repetition_penalty=1.15,
            backend_factory=cpu_factory if compute == 'CPU' else None,
            **profile['engine'],
        )
    except Exception as exc:
        emit('backend_error', backend=compute, message=str(exc), details=traceback.format_exc())
        raise
    emit('status', message=PROFILES[model][0])
    emit('ready', load_seconds=round(time.perf_counter()-startup_started, 3))
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            cmd = None
            try:
                cmd = json.loads(line)
                if cmd.get('cmd') != 'generate':
                    continue
                request_id = int(cmd['id'])
                request_started = time.perf_counter()
                text = str(cmd['text']).strip()
                if cmd.get('pronunciation', True):
                    try:
                        text = prepare_text(text)
                    except (ValueError, OSError) as exc:
                        # A mistyped user dictionary must not break live speech.
                        emit('status', message='Словарь не применён: ' + str(exc)[:130])
                text_prep = time.perf_counter() - request_started
                tempo = max(1.0, min(1.35, float(cmd.get('tempo', 1.0))))
                gain = max(0.0, min(MAX_GAIN, float(cmd.get('gain', 1.0))))
                normalize = bool(cmd.get('normalize', False))
                if not text:
                    emit('done', id=request_id)
                    continue
                # Bound the generation to avoid the Qwen "clicking forever" failure.
                limit = max(100, min(420, int(len(text) * 1.4 + 24)))
                engine.set_voice_parameters(max_new_tokens=limit, repetition_penalty=1.15)
                began = time.perf_counter()
                # queued_at is a monotonic timestamp from the GUI; never
                # subtract it from a different clock's performance counter.
                queue_wait = max(0., time.monotonic()-float(cmd.get('queued_at', time.monotonic())))
                seen_audio = False
                seen_generated = False
                last_progress_at = 0.0
                pcm_bytes = 0

                def generated_cb():
                    nonlocal seen_generated, last_progress_at
                    now = time.monotonic()
                    if now-last_progress_at >= 1:
                        emit('progress', id=request_id)
                        last_progress_at = now
                    if not seen_generated:
                        seen_generated = True
                        emit('first_generated', id=request_id,
                             seconds=round(time.perf_counter()-began, 3))

                def chunk_cb(chunk):
                    nonlocal seen_audio
                    if not seen_audio:
                        seen_audio = True
                        emit('first_chunk', id=request_id,
                             seconds=round(time.perf_counter()-began, 3),
                             text_prep=round(text_prep, 3),
                             queue_wait=round(queue_wait, 3))

                stream = TextToAudioStream(engine, output_device_index=audio_idx)
                emit('request_started', id=request_id, chars=len(text))
                stream.feed(text)
                if tempo > 1.001 or abs(gain - 1.0) > 0.001 or normalize:
                    audio_seconds = play_adjusted(stream, tempo, gain, normalize, audio_idx,
                                                  generated_cb, lambda: chunk_cb(b'x'))
                else:
                    # Normal speed follows the identical playback path that
                    # already works on the user's PC.
                    def passthrough_cb(chunk):
                        nonlocal pcm_bytes
                        pcm_bytes += len(chunk)
                        generated_cb()
                        chunk_cb(chunk)
                    stream.play(on_audio_chunk=passthrough_cb)
                    player_audio = stream.player.audio_stream
                    rate = int(player_audio.actual_sample_rate or player_audio.config.rate)
                    audio_seconds = pcm_bytes / (2 * rate)
                if engine.last_error is not None:
                    raise RuntimeError(str(engine.last_error))
                if not seen_audio or audio_seconds <= 0:
                    raise RuntimeError('native Qwen завершил запрос без звука: ни одного воспроизведённого аудиоблока')
                elapsed = time.perf_counter()-began
                emit('done', id=request_id, elapsed=round(elapsed, 3), backend=compute,
                     chars_per_second=round(len(text)/max(elapsed, 0.001), 2),
                     audio_seconds=round(audio_seconds, 3),
                     playback_rtf=round(elapsed/max(audio_seconds, 0.001), 3))
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                emit('error', id=cmd.get('id') if isinstance(cmd, dict) else None,
                     message=str(exc), details=traceback.format_exc(), backend=compute,
                     cuda_failure=compute == 'CUDA' and any(
                         word in str(exc).lower() for word in ('cuda', 'cublas', 'gpu', 'native')))
    finally:
        try:
            engine.shutdown()
        except Exception:
            traceback.print_exc(file=sys.stderr)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit('error', message='Ошибка запуска Qwen: ' + str(exc)[:300])
        sys.exit(1)
