# Cars/ — engine-sound packs (bring your own)

Engine-sound packs and car artwork are copyrighted by their authors, so this
folder ships empty (everything except this file is `.gitignore`d). LaLaDash
runs fine without it; the Modding page simply lists no cars.

## Layout

```
Cars/
  <Brand>/
    logo.png                      brand logo (optional)
    <Model>/
      side.png                    preview image (side.jpg / preview.png also work)
      engine_on_<rpm>.wav         loop per RPM band, e.g. engine_on_950.wav
      engine_off_<rpm>.wav        coast loops, RPM-keyed (coast_<rpm>.wav also works)
      engine_start.wav
      turbo_*.wav  transmission*.wav  pop_*.wav  flutter_*.wav
      config.json                 optional, overrides the filename auto-detection
```

Bands, coast sequences and effects are auto-detected from the file names.
`config.json` may set `engine_sequence_in`, `engine_sequence_off`, `pop_sounds`,
`flutter`, `gear_up`, `gear_down` and `engine_start`
(see `Modules/Modding/audio_engine.py`, `load_car_folder`).

The selected pack is stored as `[SoundFeatures] last_car = Brand/Model`.
Only use sounds you have the right to use.
