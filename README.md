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

| preset  | resolução máx | fps | bitrate |
|---------|---------------|-----|---------|
| ultra   | 2160p         | 60  | 40 Mbps |
| high    | 1080p         | 60  | 15 Mbps |
| medium  | 900p          | 60  | 8 Mbps  |
| low     | 720p          | 30  | 4 Mbps  |
| potato  | 540p          | 30  | 2 Mbps  |

Dá pra trocar em tempo real pelo menu da página (o encoder reinicia na hora).

### Qualidade de áudio

| preset | bitrate | uso          |
|--------|---------|--------------|
| alta   | 256 kbps | música      |
| media  | 128 kbps | geral (padrão) |
| baixa  | 64 kbps  | rede fraca  |
| voz    | 48 kbps  | otimizado p/ voz (VoIP) |

---

## Controles na página

- **Mouse/touch**: mover/clicar direto na tela. No mobile, *tap* = clique,
  arraste = mover o cursor.
- **🔊 som**: liga/desliga o áudio. Começa desligado (políticas de autoplay
  exigem um clique); ao ligar aparece o seletor de qualidade de áudio.
- **🎯 mouse**: ativa *pointer lock* (mouse relativo) - ideal para jogos/FPS.
- **⌨**: abre o teclado virtual no mobile.
- **⛶**: tela cheia.
- **Gamepad**: conecte um controle Xbox no device; é detectado e repassado
  automaticamente.
- **qualidade / monitor / áudio**: troca preset de vídeo, qual monitor capturar
  e a qualidade do som.

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
