# Recorded voice clips (optional)

The app plays a clip from `voices/<language>/<name>.wav` when the file exists. Without a clip it reads the same line with a voice installed in Windows, and says nothing if no voice for that language is installed.

## How to add clips

1. Generate each line below in your voice tool (calm, steady pace; keep warning lines firm but not shouted).
2. Save each clip as a **WAV** file (PCM, 16-bit, 44.1 kHz or 48 kHz, mono) with the exact file name shown. The app plays clips with Windows `winsound`, which reads WAV only.
   Convert an MP3 with: `ffmpeg -i in.mp3 -ac 1 -ar 44100 -c:a pcm_s16le out.wav`
3. Put the files in `voices/en/` and `voices/ja/`, then rebuild (BUILD.md already adds the `voices` folder).
4. Keep each clip under about 4 seconds. A warning clip starts about 1.6 seconds after the chime and replaces the rest of it.

Check the licence of the voice you use allows shipping the audio inside an app.

## English (`voices/en/`)

| File | Line |
|---|---|
| `warning_strong.wav` | Strong shaking expected. Take cover now. |
| `warning_arrived.wav` | Strong shaking arriving. Take cover now. |
| `forecast.wav` | Earthquake early warning. Shaking expected. |
| `felt_1.wav` | Shindo 1 felt. |
| `felt_2.wav` | Shindo 2 felt. |
| `felt_3.wav` | Shindo 3 felt. |
| `felt_4.wav` | Shindo 4 felt. |
| `felt_5m.wav` | Shindo 5 lower felt. |
| `felt_5p.wav` | Shindo 5 upper felt. |
| `felt_6m.wav` | Shindo 6 lower felt. |
| `felt_6p.wav` | Shindo 6 upper felt. |
| `felt_7.wav` | Shindo 7 felt. |

## Japanese (`voices/ja/`)

| File | Line |
|---|---|
| `warning_strong.wav` | 強い揺れが来ます。身の安全を確保してください。 |
| `warning_arrived.wav` | 強い揺れが到達しています。身の安全を確保してください。 |
| `forecast.wav` | 緊急地震速報です。揺れが予想されます。 |
| `felt_1.wav` | 震度1を観測しました。 |
| `felt_2.wav` | 震度2を観測しました。 |
| `felt_3.wav` | 震度3を観測しました。 |
| `felt_4.wav` | 震度4を観測しました。 |
| `felt_5m.wav` | 震度5弱を観測しました。 |
| `felt_5p.wav` | 震度5強を観測しました。 |
| `felt_6m.wav` | 震度6弱を観測しました。 |
| `felt_6p.wav` | 震度6強を観測しました。 |
| `felt_7.wav` | 震度7を観測しました。 |
