# remote-link

Compartilhe a tela do seu **Ubuntu** para **qualquer device da rede local**
(tablet, celular Android, outro notebook/Ubuntu) **pelo navegador**, com
**controle de mouse, teclado e gamepad Xbox** - baixa latência e FPS alto.

Um comando, sem instalar nada no device cliente:

```bash
./remote-link.sh
```

Abra o endereço que aparecer (ex.: `https://192.168.0.101:8443`) no navegador
do outro aparelho. Pronto.

---

## Como funciona (a escolha técnica)

Em vez de VNC (lento) ou de montar um stack GStreamer/WebRTC gigante, o
remote-link usa um caminho moderno e enxuto que entrega **latência baixíssima**:

```
tela X11 ──ffmpeg──> H.264 por HARDWARE ──WebSocket──> WebCodecs ──> <canvas>
            (NVENC / VAAPI / x264)         (Annex-B AU)   (decode HW no device)
áudio    ──ffmpeg──> Opus ──────────────> WebSocket ──> WebCodecs ──> AudioContext
            (do monitor do sink)            (1 pkt/msg)   (AudioDecoder)
```

- **Encode por hardware**: detecta sozinho o melhor encoder - **NVENC**
  (NVIDIA), **VAAPI** (Intel/AMD) ou **x264** (CPU, fallback). Tunado para
  *ultra low latency* (sem B-frames, CBR, GOP curto).
- **WebCodecs** no navegador faz o **decode por hardware** quadro a quadro,
  sem o buffer que o MSE/HLS adicionam → o menor input-lag possível num browser.
- Cada *Access Unit* H.264 é delimitado por um AUD, então o stream é fatiado
  trivialmente e cada AU vira um `EncodedVideoChunk`.
- **Áudio**: captura o som que sai do PC (monitor do sink PulseAudio/PipeWire),
  encoda em **Opus** e o navegador decodifica com `AudioDecoder` tocando via
  `AudioContext`. Buffer de jitter curto (~60 ms) e qualidade ajustável.
- **Controle** volta por outro WebSocket e é injetado no Ubuntu via
  `/dev/uinput`: mouse absoluto, teclado completo e um **gamepad Xbox 360
  virtual** (VID `045e:028e`) que jogos e a Steam reconhecem como real.

Conecte o controle Xbox no celular/notebook → ele controla o PC Ubuntu.

---

## Requisitos (no PC Ubuntu que compartilha)

- `ffmpeg`, `openssl`, `python3` (≥ 3.10)
- Python `aiohttp` + `evdev` (o script instala num venv se faltarem)
- Sessão **X11** (Xorg). Em Wayland puro a captura via `x11grab` não funciona.

No **device cliente** basta um navegador com WebCodecs (**Chrome/Edge**,
desktop ou Android). Safari/iOS tem suporte parcial.

---

## Uso

```bash
./remote-link.sh                 # inicia (porta 8443)
./remote-link.sh --port 9000     # outra porta
./remote-link.sh setup           # libera /dev/uinput (uma vez, com sudo)
```

Variáveis de ambiente úteis:

| var            | efeito                                            |
|----------------|---------------------------------------------------|
| `RL_PRESET`       | qualidade de vídeo inicial: `ultra`/`high`/`medium`/`low`/`potato` |
| `RL_ENCODER`      | força `nvenc`/`vaapi`/`x264`                    |
| `RL_AUDIO_PRESET` | qualidade de áudio inicial: `alta`/`media`/`baixa`/`voz` |
| `RL_AUDIO_SOURCE` | força a fonte de áudio (padrão segue o sink default) |
| `RL_PORT`         | porta                                          |
| `RL_DISPLAY`      | display X (auto-detectado)                      |

### Qualidade × FPS

A qualidade é um **estado único**: o nível escolhido na página ajusta vídeo
**e** áudio juntos (o encoder reinicia na hora). A faixa vai de nítido a
ultra-leve - quanto mais baixo, mais leve a transmissão e menos lag (e o áudio
acompanha). `fps` fica em 60 até `low`; só `potato` cai pra 30 (piso pra rede
ruim). Como a fonte normalmente é 1080p, pedir mais que isso não "sobe" nada -
por isso o topo é 1080p nítido.

| nível   | resolução máx | fps | bitrate vídeo | áudio          |
|---------|---------------|-----|---------------|----------------|
| ultra   | 1080p         | 60  | 25 Mbps       | alta (256 kbps) |
| high    | 900p          | 60  | 12 Mbps       | alta (256 kbps) |
| medium  | 720p          | 60  | 7 Mbps        | media (128 kbps) |
| low     | 540p          | 60  | 3,5 Mbps      | baixa (64 kbps) |
| potato  | 360p          | 30  | 1,2 Mbps      | voz (48 kbps)  |

---

## Controles na página

- **Mouse/touch**: mover/clicar direto na tela. No mobile, *tap* = clique,
  arraste = mover o cursor.
- **🔊 som**: liga/desliga o áudio. Começa desligado (políticas de autoplay
  exigem um clique). A qualidade do som segue o nível de qualidade geral.
- **🎯 mouse**: ativa *pointer lock* (mouse relativo) - ideal para jogos/FPS.
- **⌨**: abre o teclado virtual no mobile.
- **⛶**: tela cheia.
- **Gamepad**: conecte um controle Xbox no device; é detectado e repassado
  automaticamente.
- **qualidade / monitor**: troca o nível de qualidade (vídeo + áudio juntos) e
  qual monitor capturar.

---

## Primeiro acesso (certificado)

O WebCodecs exige *secure context*, então o servidor sobe em **HTTPS** com um
certificado self-signed (gerado em `certs/`). Na 1ª vez o navegador mostra um
aviso - aceite ("avançado → prosseguir"). Depois disso fica liso.

---

## Permissão do /dev/uinput

O controle precisa escrever em `/dev/uinput`. Se o servidor avisar que não tem
permissão, rode uma vez:

```bash
./remote-link.sh setup     # instala udev rule + adiciona você ao grupo input
```

(pode ser preciso logout/login uma vez para o grupo valer).

---

## Limitações conhecidas

- Captura de vídeo é **X11** (`x11grab`); Wayland puro não é suportado.
- Áudio captura o **mix do sistema** (sink default); seleção de app específico
  ainda não tem UI (use `RL_AUDIO_SOURCE` p/ forçar uma fonte).
- Self-signed: o aviso de certificado é esperado em rede local.
