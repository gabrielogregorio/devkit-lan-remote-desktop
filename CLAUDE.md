# CLAUDE.md - remote-link

Guia operacional para qualquer agente (Claude Code) trabalhar neste repositório.
Leia antes de editar.

## O que é

Ferramenta para **compartilhar a tela do PC Ubuntu** (este host) para **qualquer
device da rede local pelo navegador** (tablet, Android, outro Ubuntu/notebook),
com **controle de mouse, teclado e gamepad Xbox**. Foco: **FPS alto e o menor
input-lag possível** na LAN, acessível com um comando: `./remote-link.sh`.

## Arquitetura (a decisão central - não regrida pra VNC/WebRTC sem motivo)

```
tela X11 ──ffmpeg H.264 HW──> Access Units (AUD) ──WSS /ws/video──> WebCodecs ──> <canvas>
  (NVENC > VAAPI > x264)                                            (decode HW no device)

som  ──ffmpeg Opus──> 1 pkt Opus/msg ──WSS /ws/audio──> AudioDecoder ──> AudioContext
  (monitor do sink)                                       (WebCodecs)

device  ──mouse/teclado/gamepad (JSON)──> WSS /ws/input ──> /dev/uinput (evdev) ──> Ubuntu
```

Por quê:
- **WebCodecs** (decode quadro-a-quadro) evita o buffer do MSE/HLS → menor lag
  num browser, sem instalar nada no cliente.
- Cada Access Unit H.264 é delimitado por **AUD** (NAL type 9), então o stream é
  fatiado trivialmente e cada AU vira um `EncodedVideoChunk`.
- **Encode por hardware** auto-detectado, tunado *ultra-low-latency*
  (sem B-frames, CBR, GOP ~1s para ressync rápido de novos clientes).
- **Áudio**: ffmpeg só muxa Opus em Ogg, então `audio.py` tem um **parser Ogg
  incremental** (`OggOpusParser`) que remonta os pacotes via lacing e descarta
  os 2 headers (OpusHead/OpusTags) - cada pacote Opus vira um `EncodedAudioChunk`.
  A fonte usa o token `@DEFAULT_MONITOR@` (segue o sink default sem depender de
  `pactl`, que não existe em PipeWire puro). Cliente agenda playback com jitter
  buffer curto (~60 ms).
- **uinput** dá um **gamepad Xbox 360 virtual** (`045e:028e`) que jogos/Steam
  reconhecem como real.

## Layout

```
remote-link.sh      # único entrypoint: detecta display/IP, venv, cert TLS, sobe o server
server/
  main.py           # aiohttp: HTTPS + WSS /ws/video /ws/audio /ws/input; Hub + AudioHub
  video.py          # detect_encoder(), Presets, get_monitors(), ScreenEncoder (ffmpeg)
  inject.py         # Injector: pointer absoluto + teclado + gamepad Xbox360 via uinput
  keymap.py         # KeyboardEvent.code -> ecodes evdev
  audio.py          # AudioEncoder (ffmpeg Opus), OggOpusParser, AudioPresets, list_monitors()
  guardian.py       # escape hatch anti-ban: bloqueia/derruba se achar anticheat/jogo online
web/
  index.html / style.css / app.js   # cliente WebCodecs (sem build, JS puro)
requirements.txt    # aiohttp, evdev
certs/  .venv/       # gerados em runtime - NÃO versionar (estão no .gitignore)
```

## Como rodar / testar

```bash
./remote-link.sh                 # produção (porta 8443)
./remote-link.sh --port 9000
./remote-link.sh setup           # libera /dev/uinput (uma vez, com sudo)

# checagens de sintaxe rápidas:
python3 -m py_compile server/*.py
node --check web/app.js
bash -n remote-link.sh
```

Variáveis: `RL_PRESET` (ultra/high/medium/low/potato), `RL_ENCODER`
(nvenc/vaapi/x264), `RL_PORT`, `RL_DISPLAY`, `RL_XAUTHORITY`, `RL_HOST`,
`RL_CERT`, `RL_KEY`, `RL_RENDER_NODE`, `RL_GUARDIAN` (`off` desliga o escape
hatch anti-ban - ver `ANTIBAN.md`).

### Cuidado ao testar localmente
- WebCodecs exige **secure context** → o server só serve sentido em **HTTPS**.
  Sem cert, ele avisa e WebCodecs não roda fora de `localhost`.
- O encoder **só liga quando há ≥1 espectador** conectado em `/ws/video` (e
  desliga quando o último sai). Não espere ffmpeg rodando sem cliente.
- O render WebCodecs no browser precisa de um navegador real para validação
  visual - `py_compile`/`node --check` só pegam sintaxe.

## Endpoints HTTP

- `GET /api/monitors` - config atual: encoder, monitores, presets, `video`
  (res/fps/bitrate atuais), faixas (`res_options`/`fps_options`/`bitrate_options`),
  presets de áudio e `audio_manual`.
- `GET /api/gamepad-map` - todos os perfis de remapeamento salvos (por
  `gamepad.id`). `POST` com `{"id","map"}` salva um perfil. Persistido por
  `server/gamepad.py` em `~/.config/remote-link/gamepad-map.json` (ou
  `RL_GAMEPAD_MAP`). O cliente traduz o pad bruto → formato Xbox padrão antes de
  enviar `t:"g"`; o servidor só guarda/serve o JSON (blob opaco).

## Protocolo dos WebSockets

`/ws/video` (binário): servidor → cliente, cada mensagem = 1 Access Unit H.264
Annex-B. Mensagens **texto** = controle, ex.: `{"t":"reset"}` (cliente deve
recriar o `VideoDecoder` e esperar o próximo keyframe).

`/ws/audio` (binário): servidor → cliente, cada mensagem = 1 pacote Opus
(48 kHz, estéreo, frame de 20 ms). Cliente decoda com `AudioDecoder` e agenda
no `AudioContext`. Liga só com ≥1 ouvinte conectado.

`/ws/input` (texto JSON): cliente → servidor.
- `{"t":"m","x":0..1,"y":0..1}` mover mouse (absoluto, normalizado na região)
- `{"t":"mr","dx","dy"}` mover relativo (pointer-lock)
- `{"t":"md"/"mu","b":N}` botão down/up
- `{"t":"w","x","y"}` scroll
- `{"t":"k","code":"KeyA","down":true}` tecla
- `{"t":"g","a":[lx,ly,rx,ry],"b":[{"p":bool,"v":float},...],"lt":0..1,"rt":0..1}`
  gamepad. `lt`/`rt` são os gatilhos enviados explícitos (o cliente detecta
  mesmo em mapping não-standard, ex.: controle PS2/USB onde vêm como eixos); o
  servidor cai pros `b[6]/b[7]` só se ausentes.
- `{"t":"q","preset":"high"}` atalho de preset: aplica resolução+fps+bitrate de
  uma vez. O áudio acompanha (`main.AUDIO_FOR_QUALITY`) **só** se o usuário ainda
  não tiver mexido no áudio na mão (`AudioHub.manual`). Reinicia os encoders.
- `{"t":"vq","res":720,"fps":60,"bitrate":"7M"}` qualidade de **vídeo
  independente** (qualquer subconjunto dos 3 campos); vira preset "custom".
- `{"t":"aq","preset":"media"}` qualidade de **áudio independente** (marca
  `AudioHub.manual=True` → desliga o arrasto do `q`).
- `{"t":"mon","i":0}` trocar monitor (`i:null` = primário)

Encoder H.264 tunado p/ baixa latência: GOP ~1s (ressync rápido p/ cliente novo
sem tela preta longa; na LAN banda sobra), VBV pequeno (`_lowlat_bufsize` ~1/3
do bitrate/s), spatial-AQ no NVENC (nitidez no mesmo bitrate), profile `high`,
`no-scenecut`+`forced-idr`. O encode (NVENC) sustenta 1080p60 a 1x; o
gargalo prático costuma ser o **decode no browser**, por isso a régua útil é
descer resolução/bitrate (controles separados).

O cliente (`web/app.js`) **descarta delta frames até o 1º keyframe** após
`configure()`/reset (`needKey`): o `VideoDecoder` exige keyframe como 1º chunk,
senão dá erro "key frame is required" no Firefox e no Chrome.

## Convenções

- Comentários e mensagens ao usuário em **português** (o resto do projeto está
  assim). Código/identificadores em inglês quando natural.
- Cliente web é **JS puro, sem build/bundler** - mantenha assim.
- Latência acima de features. Antes de adicionar buffering/qualidade, pense no lag.
- Nunca versione `certs/` (chave privada) nem `.venv/`.

## Próximos passos possíveis (ainda não feitos)

- Seleção de **app de áudio** específico na UI (hoje captura o mix do sink).
- Modo "desktop inteiro" (todos os monitores num único stream).
- Captura **Wayland** via PipeWire portal (hoje só X11/`x11grab`).
- Bitrate adaptativo por RTT/perda (vídeo e áudio).
- Autenticação (PIN/token) - hoje é LAN aberta atrás do TLS self-signed.

## Feito
- **Áudio**: canal Opus via `/ws/audio` com qualidade ajustável em runtime
  (presets `alta`/`media`/`baixa`/`voz`). Ver `server/audio.py`.
- **Escape hatch anti-banimento** (`server/guardian.py`): roda **antes** de
  carregar qualquer lib de controle/tela. Não inicia (e derruba em runtime) se
  detectar anticheat (Vanguard, EAC, BattlEye, GameGuard…) ou jogo online da
  lista - em Windows (`tasklist`) e Linux (`/proc`, inclui Proton/Wine). Mostra
  aviso na tela (diálogo + terminal) dizendo o quê e por quê. Desliga com
  `RL_GUARDIAN=off` (não recomendado). Detalhes e lista completa em
  **`ANTIBAN.md`** / `python -m server.guardian --list`. Variável: `RL_GUARDIAN`.
