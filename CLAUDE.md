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
  video.py          # detect_encoder(), Presets, get_monitors(), ScreenEncoder (ffmpeg),
                    #   saneadores do canal 'vq' (clamp_res/clamp_fps/clean_bitrate)
  inject.py         # Injector: pointer absoluto + teclado + gamepad Xbox360 via uinput
  keymap.py         # KeyboardEvent.code -> ecodes evdev
  audio.py          # AudioEncoder (ffmpeg Opus), OggOpusParser, AudioPresets, list_monitors()
  gamepad.py        # persistência dos perfis de remap (~/.config/remote-link/gamepad-map.json)
  guardian.py       # escape hatch anti-ban: bloqueia/derruba se achar anticheat/jogo online
web/
  index.html / style.css / app.js   # cliente WebCodecs (sem build, JS puro)
  gamepad-debug.html                # página de diagnóstico de gamepad (script inline)
tests/              # pytest (servidor): unit, integração, fuzz, parsers, script, docs
web/__tests__/      # vitest + jsdom (cliente)
web/e2e/            # playwright (servidor real + browser real)
pytest.ini  .coveragerc  vitest.config.js  playwright.config.js  package.json
requirements.txt    # aiohttp, evdev
certs/  .venv/  node_modules/   # gerados em runtime - NÃO versionar (estão no .gitignore)
```

## Como rodar / testar

```bash
./remote-link.sh                 # produção (porta 8443)
./remote-link.sh --port 9000
./remote-link.sh setup           # libera /dev/uinput (uma vez, com sudo)
```

**Antes de commitar, rode as três suítes** (detalhes e mapa do que cada arquivo
cobre em **`TESTING.md`**):

```bash
./.venv/bin/python -m pytest --cov=server    # servidor: gate de 100% (.coveragerc)
npm run test:web                             # cliente (vitest+jsdom): gate no vitest.config.js
npm run test:e2e                             # ponta a ponta (sobe o server real + Chromium)
```

Checagens de sintaxe (rápidas, mas **não substituem** as suítes):

```bash
python3 -m py_compile server/*.py && node --check web/app.js && bash -n remote-link.sh
```

| variável | efeito |
|---|---|
| `RL_PRESET` | preset inicial de vídeo: `ultra`/`high`/`medium`/`low`/`potato` |
| `RL_ENCODER` | força `nvenc`/`vaapi`/`x264` (pula o autodetect) |
| `RL_RENDER_NODE` | nó DRM do VAAPI (padrão `/dev/dri/renderD128`) |
| `RL_AUDIO_PRESET` | preset inicial de áudio: `alta`/`media`/`baixa`/`voz` |
| `RL_AUDIO_SOURCE` | força a fonte de áudio (padrão `@DEFAULT_MONITOR@`) |
| `RL_PORT` / `RL_HOST` | porta e bind (padrão `8443` / `0.0.0.0`) |
| `RL_CERT` / `RL_KEY` | caminho do par TLS (o `.sh` gera em `certs/` se faltar) |
| `RL_DISPLAY` / `RL_XAUTHORITY` | display X (o `.sh` autodetecta pelo `gnome-shell`) |
| `RL_GAMEPAD_MAP` | arquivo dos perfis de remap (padrão `~/.config/remote-link/`) |
| `RL_GUARDIAN` | `off` desliga o escape hatch anti-ban - ver `ANTIBAN.md` |
| `RL_GUARDIAN_INTERVAL` | intervalo (s) da varredura do guardian (padrão `2.0`) |

Os testes usam `RL_GUARDIAN=off` e `RL_ENCODER=x264` (`tests/conftest.py`) pra
não depender do hardware nem do escape hatch.

### Cuidado ao testar localmente
- WebCodecs exige **secure context** → o server só serve sentido em **HTTPS**.
  Sem cert, ele avisa e WebCodecs não roda fora de `localhost`.
- O encoder **só liga quando há ≥1 espectador** conectado em `/ws/video` (e
  desliga quando o último sai). Não espere ffmpeg rodando sem cliente.
- O render WebCodecs no browser precisa de um navegador real para validação
  visual - `py_compile`/`node --check` só pegam sintaxe.
- `pytest` e `vitest` rodam sem X11, sem GPU e sem `/dev/uinput` (tudo dublado).
  Só o `npm run test:e2e` sobe servidor de verdade; o teste que captura tela se
  auto-pula sem `DISPLAY`.
- `web/app.js` termina com um **seam de teste**: `module.exports = {...}` quando
  existe `module` (Node/vitest), senão `boot()`. **Não remova** - é o que
  permite testar o cliente sem bundler. Função nova que precise de teste entra
  nessa lista.

## Endpoints HTTP

- `GET /api/monitors` - config atual: encoder, monitores, presets, `video`
  (res/fps/bitrate atuais), faixas (`res_options`/`fps_options`/`bitrate_options`),
  presets de áudio e `audio_manual`.
- `GET /api/gamepad-map` - todos os perfis de remapeamento salvos (por
  `gamepad.id`). `POST` com `{"id","map"}` salva; `DELETE` com `{"id"}` apaga.
  Persistido por `server/gamepad.py` em
  `~/.config/remote-link/gamepad-map.json` (ou `RL_GAMEPAD_MAP`). O servidor só
  guarda/serve o JSON (blob opaco).
- `GET /gamepad-debug.html` - página de diagnóstico: mostra `mapping`, todos os
  `buttons[i].value` e `axes[i]` ao vivo. Use pra descobrir como um controle
  aparece antes de mexer em mapeamento.

Gamepad - **passagem direta por padrão**: se `gamepad.mapping === "standard"`
(Xbox e a maioria), o cliente repassa byte a byte na ordem W3C (que já é a ordem
que o servidor quer) e **ignora qualquer perfil** - um Xbox nunca precisa de
remap. O remap (`🎮`) só vale e só roda em controle **não-standard** (clones
PS2/USB); `🎮↺` apaga o perfil e volta à passagem direta. Gatilhos LT/RT são
replicados fiéis (`clamp01` no cliente, `round(v*255)` no servidor) - sem
deadzone/rescale.

O `pollPads()` só reenvia quando o estado muda **na resolução do destino**
(eixo → int16, botão/gatilho → byte 0..255): o valor que vai no fio continua
sendo o cru, mas comparar float faria o micro-ruído do controle gerar
mensagem 60×/s à toa.

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
- `{"t":"md","b":N}` / `{"t":"mu","b":N}` botão do mouse down/up
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

### Robustez do canal de input (regra, não detalhe)

O `/ws/input` é **LAN aberta**: qualquer coisa pode chegar nele, e uma exceção
que escape do handler **derruba o WebSocket inteiro** - o usuário perde mouse e
teclado até o cliente reconectar. Por isso:

- payload que não é objeto JSON (`[]`, `5`, `null`, `"txt"`) é **ignorado**;
- `preset` não-string é ignorado (`x in DICT` com lista/dict levanta
  `TypeError: unhashable`);
- `vq` passa por `video.clamp_res` / `clamp_fps` / `clean_bitrate`: fora da
  faixa é **clampado**, lixo **mantém o valor atual**. Faixas: altura
  144–2160, fps 5–240, bitrate 100 kbps–100 Mbps no formato do ffmpeg
  (`12M`/`3500k`/`800000`);
- `mon` com índice não-inteiro é ignorado;
- o resto vai pro `injector.handle()` dentro de `try/except` (mouse/teclado
  falham limpo antes de escrever qualquer coisa);
- `gamepad_state()` é **total**: tipo errado/NaN/campo faltando vira neutro
  (solto/centro) e o frame **sempre** termina em `syn()` - meio-frame escrito
  é o que gera botão preso e gatilho fantasma;
- `/api/gamepad-map` responde **400** (não 500) pra JSON que não seja objeto.

Cliente (`web/app.js`) faz o simétrico: `padNum`/`clamp01` garantem que nunca
sai NaN/Infinity no JSON, e `pollPads()` roda dentro de `try/catch` - se a
exceção escapar, o `requestAnimationFrame` do fim não roda e **o gamepad morre
pelo resto da sessão**. Ao mexer nesses caminhos, rode
`tests/test_fuzz_input.py` e `web/__tests__/protocol.test.js`.

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
- **Todo campo novo do protocolo nasce com saneamento e teste**: um valor
  inesperado não pode derrubar conexão nem encoder (ver "Robustez" acima).
- **Doc junto com o código.** `tests/test_docs_sync.py` falha se um endpoint,
  um tipo de mensagem, um preset ou uma `RL_*` existir no código e não aparecer
  aqui/no README. Se o teste reclamar, a doc é que está errada.
- Cobertura do servidor tem gate de **100%** (`.coveragerc`); o cliente tem
  limiares no `vitest.config.js`. Código novo vem com teste no mesmo commit.

## Próximos passos possíveis (ainda não feitos)

- Seleção de **app de áudio** específico na UI (hoje captura o mix do sink).
- Modo "desktop inteiro" (todos os monitores num único stream).
- Captura **Wayland** via PipeWire portal (hoje só X11/`x11grab`).
- Bitrate adaptativo por RTT/perda (vídeo e áudio).
- Autenticação (PIN/token) - hoje é LAN aberta atrás do TLS self-signed.

## Feito
- **Suíte de testes em 4 camadas** (`TESTING.md`): unit (pytest/vitest),
  integração real de WebSocket (`tests/test_integration_ws.py`), fuzz e
  propriedade (`test_fuzz_input.py`, `test_parsers_property.py`,
  `web/__tests__/protocol.test.js`), entrypoint em sandbox
  (`tests/test_script.py`), e2e no browser (`web/e2e/`). Gate de 100% no
  servidor; guarda anti-drift da doc em `tests/test_docs_sync.py`.
- **Endurecimento contra entrada hostil**: `/ws/input` e `/api/gamepad-map`
  ignoram/recusam payload torto em vez de derrubar a conexão; `vq` é clampado;
  `gamepad_state()` virou total; no cliente, `pollPads()` não morre mais se o
  controle entregar valor inválido (ver "Robustez do canal de input").
- **Áudio**: canal Opus via `/ws/audio` com qualidade ajustável em runtime
  (presets `alta`/`media`/`baixa`/`voz`). Ver `server/audio.py`.
- **Escape hatch anti-banimento** (`server/guardian.py`): roda **antes** de
  carregar qualquer lib de controle/tela. Não inicia (e derruba em runtime) se
  detectar anticheat (Vanguard, EAC, BattlEye, GameGuard…) ou jogo online da
  lista - em Windows (`tasklist`) e Linux (`/proc`, inclui Proton/Wine). Mostra
  aviso na tela (diálogo + terminal) dizendo o quê e por quê. Desliga com
  `RL_GUARDIAN=off` (não recomendado). Detalhes e lista completa em
  **`ANTIBAN.md`** / `python -m server.guardian --list`. Variável: `RL_GUARDIAN`.
