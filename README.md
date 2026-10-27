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
| `RL_GAMEPAD_MAP`  | onde salvar os perfis de remap de gamepad       |
| `RL_GUARDIAN`     | `off` desliga a trava anti-banimento (veja `ANTIBAN.md`) |

### Qualidade: um atalho + três controles finos

O seletor de **qualidade** é um atalho: aplica resolução, fps e bitrate de uma
vez (e o áudio acompanha, enquanto você não escolher o som na mão). Ao lado
dele ficam os controles **independentes** - resolução, fps, bitrate e áudio -
pra afinar cada um por si. Mexeu em qualquer um, o encoder reinicia na hora e a
página vira "custom".

| atalho  | resolução máx | fps | bitrate vídeo | áudio junto     |
|---------|---------------|-----|---------------|-----------------|
| ultra   | 1080p         | 60  | 25 Mbps       | alta (256 kbps) |
| high    | 900p          | 60  | 12 Mbps       | alta (256 kbps) |
| medium  | 720p          | 60  | 7 Mbps        | media (128 kbps) |
| low     | 540p          | 60  | 3,5 Mbps      | baixa (64 kbps) |
| potato  | 360p          | 30  | 1,2 Mbps      | voz (48 kbps)   |

Faixas dos controles finos: resolução `360…1080p`, fps `30…120`, bitrate
`1200k…25M`. Como a fonte normalmente é 1080p, pedir mais não "sobe" nada - o
que abre a faixa de verdade é **descer** resolução e bitrate: mais leve de
decodificar no celular e menos lag. `fps` fica em 60 até `low`; só `potato` cai
pra 30 (piso pra rede ruim).

Escolheu o áudio na mão? A partir daí o atalho de qualidade **não mexe mais no
som** - o nível de áudio é seu.

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
  automaticamente (veja abaixo).
- **qualidade / resolução / fps / bitrate / áudio / monitor**: o atalho de
  qualidade e os controles finos de cada parâmetro, além de qual monitor
  capturar.

---

## Gamepad

Controle **Xbox (e a maioria dos modernos)**: conecte no celular/notebook e
pronto - o navegador reporta `mapping: "standard"` e o cliente **repassa direto**,
botão por botão, sem tradução nenhuma. Não precisa configurar nada.

Controle **PS2/USB "pirata"** costuma reportar mapping vazio, com botões e
gatilhos em índices arbitrários. Para esses:

- **🎮 remapear**: assistente "aperte o botão X" - passa por cada botão, os dois
  gatilhos e os dois sticks. O gatilho é **calibrado** (mede repouso e fundo),
  o que evita o clássico "gatilho meio apertado parado" que virava double click.
  O perfil é salvo **no servidor** por `gamepad.id`, então vale de qualquer
  device que acessar este host.
- **🎮↺ resetar**: apaga o perfil e volta à passagem direta.
- **`/gamepad-debug.html`**: página de diagnóstico. Mostra ao vivo `mapping`,
  cada `buttons[i]` e `axes[i]`, a faixa (min..max) já vista e um contador de
  **oscilação** - é ele que denuncia o gatilho que fica pulando sozinho. Abra
  antes de remapear pra entender o que o seu controle reporta.

Remapear um controle *standard* é bloqueado de propósito: só estragaria.

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

## Trava anti-banimento

O remote-link injeta entrada sintética e um gamepad Xbox **virtual** - pra um
anticheat isso é indistinguível de cheat. Então existe um **escape hatch**: se
houver anticheat ou jogo online da lista rodando, o servidor **não inicia**, e
se um deles abrir enquanto ele está no ar, tudo cai na hora (o controle virtual
some, o ffmpeg morre). Detalhes, lista completa e como desligar (`RL_GUARDIAN=off`,
por sua conta e risco): **[ANTIBAN.md](ANTIBAN.md)**.

---

## Desenvolvimento

```bash
./.venv/bin/python -m pytest --cov=server   # servidor (gate de 100%)
npm install && npm run test:web             # cliente (vitest + jsdom)
npm run test:e2e                            # ponta a ponta (server real + Chromium)
```

A estratégia de teste - o que cada camada cobre e como escrever teste novo -
está em **[TESTING.md](TESTING.md)**. Guia de arquitetura pra quem for mexer no
código: **[CLAUDE.md](CLAUDE.md)**.

---

## Limitações conhecidas

- Captura de vídeo é **X11** (`x11grab`); Wayland puro não é suportado.
- Áudio captura o **mix do sistema** (sink default); seleção de app específico
  ainda não tem UI (use `RL_AUDIO_SOURCE` p/ forçar uma fonte).
- Self-signed: o aviso de certificado é esperado em rede local.
- Sem autenticação: é LAN aberta atrás do TLS. Não exponha na internet.
