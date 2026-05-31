# Escape hatch anti-banimento (guardian)

`server/guardian.py` é uma **trava de segurança** que impede o remote-link de
rodar junto com jogos online/competitivos protegidos por anticheat.

## Por que isto existe

O remote-link injeta **mouse, teclado e um gamepad Xbox 360 _virtual_**
(`045e:028e`) via `/dev/uinput`, além de capturar a tela. Para um anticheat
(Vanguard, EAC, BattlEye, GameGuard, etc.) esse cenário - um "jogador"
controlado de **outro PC**, com entrada sintética e um controle que não existe
fisicamente - é indistinguível de um **cheat/automação**. Resultado provável:
**banimento da conta** (e, em casos como Vanguard/Tarkov, do HWID).

A ferramenta foi feita para **LAN casual / trabalho remoto na própria rede**,
não para jogar competitivo de outro PC. O guardian garante que ninguém faça
isso por engano e perca a conta.

## O que ele faz (3 camadas)

1. **Pré-flight no shell** - `remote-link.sh` roda `python -m server.guardian
   --check` **antes** de mexer em uinput/TLS/ffmpeg. Se a máquina não estiver
   limpa, o servidor **nem é iniciado**.
2. **Pré-flight no Python** - `server/main.py` chama `guardian.enforce_preflight()`
   na **primeira linha**, _antes_ de importar `aiohttp`, `video`, `audio` e
   `inject`. Ou seja: num PC bloqueado, **as libs de controle (evdev/uinput) e de
   tela (ffmpeg) nem chegam a ser carregadas**. Isso também cobre Windows, onde o
   `.sh` não roda.
3. **Monitor em runtime** - enquanto o servidor está no ar, varre os processos a
   cada **~2 s** (ajustável por `RL_GUARDIAN_INTERVAL`). Se um anticheat/jogo da
   lista **abrir**, derruba tudo na hora **sem pedir confirmação**: solta o
   `uinput` (o gamepad virtual **some**), mata o `ffmpeg` (a captura **para**) e
   encerra o processo (`os._exit(99)`). Janela de reação: 0–2 s - e, na prática,
   o anticheat/loader sobe antes do jogo ficar jogável, então cai com folga.

Em todos os casos mostra um **aviso na tela**: um diálogo gráfico
(`zenity`/`kdialog`/`notify-send`/`xmessage` no Linux, `MessageBox` no Windows)
**e** um banner vermelho no terminal, dizendo **qual programa/jogo** disparou e
**por quê** (a ferramenta controla mouse/tela/teclado/controle → risco de ban).

Princípio: **na dúvida, fecha.** Latência de jogo casual não vale perder a conta.

## Como detecta (Windows + Linux, sem dependência extra)

- **Linux**: lê `/proc/<pid>/{comm,exe,cmdline}`. Pega o basename do executável e
  de **cada token do cmdline** - então também detecta jogos rodando via
  **Proton/Wine** (o `.exe` aparece nos argumentos) e nomes nativos (`cs2`,
  `dota2`, `tf_linux64`).
- **Windows**: `tasklist /fo csv` (pega `vgc.exe`, `BEService.exe`,
  `EasyAntiCheat.exe`, os `.exe` dos jogos, etc.).
- **Outros (macOS/BSD)**: fallback via `ps`.

O match é por **basename exato** do executável (case-insensitive), o que evita
falso-positivo com substring (ex.: o toolchain `rust`/`cargo` **não** casa com o
jogo `Rust.exe`).

## O que está na lista

Anticheats (sinal mais forte - fecha mesmo sem o jogo aberto, ex.: `vgc.exe` do
Vanguard que sobe no boot do Windows):

> Riot Vanguard · Easy Anti-Cheat (EAC/EOS) · BattlEye · nProtect GameGuard ·
> XignCode3 · PunkBuster · FACEIT · ESEA · miHoYo ACE/mhyprot · Denuvo
> Anti-Cheat · Ricochet (CoD) · Sabreshield

Jogos online/multiplayer com risco de ban (principais do mundo):

> Valorant · League of Legends/TFT · Fortnite · CS2/CS:GO · PUBG · Apex Legends ·
> Call of Duty (MW/Warzone/BO) · Rainbow Six Siege · Overwatch 2 · Diablo IV ·
> Destiny 2 · Rust · DayZ · Escape from Tarkov · GTA V Online/FiveM · RDR2 Online ·
> Battlefield (2042/V/1/4/6) · Roblox · Genshin Impact · Honkai Star Rail ·
> Zenless Zone Zero · Wuthering Waves · Dead by Daylight · Halo Infinite/MCC ·
> Paladins · Smite · War Thunder · World of Tanks · World of Warships · Dota 2 ·
> Team Fortress 2 · Lost Ark · New World · Black Desert · ARK · The Finals ·
> Marvel Rivals · Hunt: Showdown · Sea of Thieves · For Honor · The Division 2 ·
> Warframe · Fall Guys · Naraka: Bladepoint · Delta Force · Splitgate 2 ·
> Throne and Liberty · Once Human · Gray Zone Warfare · Hell Let Loose ·
> Star Citizen · EA SPORTS FC/FIFA

Lista completa e sempre atualizada: `python -m server.guardian --list`, ou os
dicionários `ANTICHEATS` / `GAMES` em `server/guardian.py`. **Para adicionar um
jogo**, basta colocar o basename do executável (minúsculo) na lista.

## Comandos úteis

```bash
python -m server.guardian --check    # pré-flight: exit 0 = limpo, 3 = bloqueado
python -m server.guardian --list     # mostra tudo que é bloqueado
```

## Desligar (por sua conta e risco)

```bash
RL_GUARDIAN=off ./remote-link.sh
```

Aceita `off/0/false/no/nao/disable`. **Não recomendado**: sem o guardian você
fica sem qualquer proteção contra banimento. Use só se tiver 100% de certeza de
que nenhum jogo online com anticheat vai rodar nessa máquina.

## Limitações honestas

- A lista é por **nome de processo**: um jogo novo ou renomeado pode escapar até
  ser adicionado. É uma **rede de segurança**, não uma garantia absoluta - a
  responsabilidade final de não usar a ferramenta em jogos online é sua.
- Detecta o que está **na própria máquina** (a que roda o servidor e é
  controlada) - que é exatamente onde o anticheat rodaria.
- A janela do monitor é de ~2 s (`RL_GUARDIAN_INTERVAL`); o pré-flight cobre o
  caso de algo já aberto (janela zero).
