# AGENTS.md — Servicio técnico de dispositivos (turnstile_controller)

Guía para un agente (Claude Code / Codex) que hace de **soporte técnico** de las
instalaciones: alguien reporta algo en lenguaje humano («la puerta de Barbate no
abre de noche», «la cámara de Emilio en Guadix va lenta») y tú tienes que
averiguar **de qué gimnasio y de qué dispositivo** se trata, conectarte y
diagnosticar.

Cada gimnasio tiene una **puerta/torno** (lee el QR y abre) y, a veces, una
**cámara**. Los servicios son units de `systemd`; el código vive en
`~/turnstile_controller`.

## ⛔ Regla de oro

El primer encargo es **SIEMPRE solo diagnosticar y explicar qué pasa**. **No
cambies, reinicies, reconfigures ni borres NADA sin preguntar antes.** Primero
averiguas y resumes; si hay que actuar, lo propones y esperas el OK.

## ⚡ Lo PRIMERO: carga las credenciales

```bash
set -a; source .env.agents; set +a
```
**No** `cat`/`grep`/`echo` ese fichero (gitignored) ni imprimas sus valores: solo
súrcalo. Si no existe, créalo desde `.env.agents.example` con el fichero de
contraseñas. Provee: `DEVICE_SSH_*` (dispositivos), `INFRASTRUCTURE_SSH_PASSWORD`
+ `BACKEND_SSH`/`FRONTEND_SSH`/`FRP_JUMPHOST_SSH` (servidores) y `POSTGRES_DB_*`
(base de datos de producción).

## 1. Identifica el gimnasio y el dispositivo (EMPIEZA AQUÍ)

Antes de mirar ningún log tienes que saber **qué gimnasio** y **qué dispositivo**.
La fuente de la verdad es la **base de datos de producción** (acceso de lectura con
`POSTGRES_DB_*`). Los dispositivos viven en **DOS tablas — mira siempre las dos**:

- La tabla de **dispositivos ODROID** (sistema nuevo): trae los datos de **SSH** y
  el mapeo a la entrada y al gimnasio.
- La tabla de dispositivos **legacy** (Raspberry Pi y algún ODROID viejo): su
  nombre lleva **descripción con dueño y lugar** (p.ej. «torno de Emilio en
  Guadix»), un comando SSH ya montado, y su estado online/última conexión.

Traduce las palabras del reporte:
- «puerta» / «torno» / «cámara» → tipo de entrada/dispositivo.
- El **lugar** (Barbate, Guadix, Baza…) → ciudad o nombre del gimnasio.
- El **dueño** (Emilio, Hassane, Jose…) → casi siempre en la tabla legacy.

**Avisos al identificar (lecciones reales):**
- Un gimnasio puede tener **varias entradas**: «Puerta» y «Torno» son
  dispositivos distintos. No asumas que «puerta» es literal — revisa **todas** las
  entradas (a veces el problema está en el torno aunque digan «la puerta»).
- Un mismo aparato puede estar en **las dos tablas**: si la fila ODROID trae el
  SSH vacío, busca su SSH en la tabla legacy por el lugar (puede ser el mismo
  equipo en transición; algunos legacy entran con usuario **root**, no `manager`).
- «Online» = **heartbeat reciente**. Un estado/última-conexión viejo (p.ej.
  >15–30 min) trátalo como **caído** aunque el flag diga «SUCCESS/online».
- «Cámara» a veces **no es un equipo aparte** sino el vídeo de una entrada. Si no
  encuentras dispositivo-cámara, mira si la entrada tiene el **vídeo activado**
  antes de concluir que «no existe».

**Las consultas SQL exactas (conexión + esquema + joins) están en
`AGENTS.queries.md`** (fichero **local y gitignored**, fuera del repo público).
Léelo y úsalo. Si no existe en este entorno, **introspecciona el esquema** tú
mismo (`\dt`, `\d <tabla>`) con el acceso de `POSTGRES_DB_*` y reconstruye las
consultas; o pídeselo al usuario.

**Si NO estás seguro, PREGUNTA con un selector.** Si el lugar/dueño no aparece,
sale en varios gimnasios, o es ambiguo («Emilio», «Hassan», «Cele»…), **no
adivines**: presenta las coincidencias (gimnasio + ciudad) y deja que el operador
elija. Si no hay ninguna, dilo y pide más datos (¿ciudad?, ¿nombre del gimnasio o
del dueño?).

## 2. Conéctate por SSH

`ssh` normal se queda esperando la contraseña y **el agente no puede teclearla**.
Usa `sshpass` con las variables cargadas:
```bash
sshpass -p "$DEVICE_SSH_PASSWORD" ssh -o StrictHostKeyChecking=no \
  -p 6008 "$DEVICE_SSH_USER@$DEVICE_SSH_HOST" 'cd ~/turnstile_controller && <comando>'
```
- Falta `sshpass` → `sudo apt-get install -y sshpass` (o `paramiko`).
- `sudo` en el dispositivo (mismo password):
  `... ssh ... "echo \"$DEVICE_SSH_PASSWORD\" | sudo -S systemctl restart <servicio>"`.
- Para un **servidor** (backend/frontend/FRP): `sshpass -p "$INFRASTRUCTURE_SSH_PASSWORD" ssh "$BACKEND_SSH" '<cmd>'`.
- Config del dispositivo (broker, cámara, relé…): `~/turnstile_controller/.env`.

## 3. Ver logs

```bash
journalctl -u <servicio> -n 80 --no-pager        # últimas líneas
journalctl -u <servicio> --since "today"          # de hoy
journalctl -u <servicio> --since "7 days ago"     # ventana
systemctl is-active <servicio>                     # ¿activo?
```

## 4. Mapa de servicios

| Dónde | Servicio | Para qué |
|-------|----------|----------|
| Puerta/torno | `qr_script_a` (y `qr_script_b` si hay 2 lectores) | lee el QR, valida y abre (relé) |
| Puerta/torno | `mqtt-sender` | avisa a la cámara por MQTT para que grabe |
| Cámara | `mosquitto` | broker MQTT (recibe el aviso) |
| Cámara | `mqtt-receiver` | recibe el aviso y deja la señal de grabación |
| Cámara | `videorecorder` | graba ~6 s |
| Cámara | `upload` | sube el `.mp4` a S3 |
| Ambos | `device_configurator` | heartbeat + comandos del backend |
| Ambos | `frpc` | túnel al relay (da el «SSH remoto» y el online/offline) |

## 5. Caso: la PUERTA / contar entradas / fallos del día

`qr_script_a` (y `qr_script_b` si hay 2 lectores) loguea cada lectura con un
`response_code`. Diagnóstico típico («no abre / no pueden entrar»):
1. **¿Funciona la puerta?** `systemctl is-active qr_script_a` + busca líneas
   `Opening door` / `Hola, <nombre>!` recientes (abre para socios válidos).
2. **¿Lector QR conectado?** que `qr_script_a` esté logueando lecturas.
3. **¿Desconexiones?** `journalctl -u frpc` / `device_configurator`.
4. **¿Internet?** suele ser **inofensivo**: la validación es local; la puerta
   sigue abriendo aunque el túnel parpadee.
5. **Causa raíz frecuente = NO es avería, es el horario del socio.** El
   `response_code` distingue: socio OK, **fuera de horario** («Fuera de horario»),
   socio sin pagar, QR que no es de ningún socio, QR caducado. Para **contar los
   rechazos y nombrar a los clientes afectados**, usa la consulta de
   `AGENTS.queries.md` (une el log de entradas con la tabla de clientes). Resume:
   «la puerta funciona; estos socios intentan entrar fuera de su horario: <nombres
   + nº de veces>».
   **OJO:** los rechazos por horario **no salen fiables en el `journalctl` del
   dispositivo** (ahí ves sobre todo aperturas/`UserExists`); el conteo y los
   nombres se sacan de la **BD**. Y las horas de la BD están en **UTC** →
   conviértelas a hora local (España = **UTC+2** en verano) antes de hablar de
   «noche/madrugada».

## 6. Caso: la CÁMARA no graba (o graba tarde)

El flujo cruza **dos** dispositivos → SSH a **la cámara** y al **disparador**.
Cadena: `qr_script_a` deja un fichero → `mqtt-sender` publica a `MQTT_BROKER` (IP de
la cámara) → `mosquitto` → `mqtt-receiver` escribe `record.txt` → `videorecorder`
graba → `upload` sube a S3.
```bash
# Disparador:
journalctl -u qr_script_a -n 50 --no-pager           # ¿leyó el QR?
journalctl -u mqtt-sender -n 50 --no-pager           # ¿"Found trigger file" + "Sent payload"? ¿a qué broker?
grep MQTT_BROKER ~/turnstile_controller/.env          # ¿IP correcta de la cámara?
# Cámara:
journalctl -u mqtt-receiver -n 50 --no-pager          # ¿"Received data for UUID"?
journalctl -u videorecorder -n 50 --no-pager          # ¿"Started recording"? ¿errores?
journalctl -u upload -n 50 --no-pager                 # ¿subió a S3?
```
- «Reacciona tarde / el cliente ya pasó» → latencia. Los ODROID actuales usan
  `inotify` (despiertan en ~1 ms); comprueba si la cámara tiene el código nuevo
  (`grep -c DirectoryWatcher mqtt_sender.py`) y si el WiFi reconecta el MQTT en
  cada disparo.
- Envía pero no recibe → `MQTT_BROKER` mal (la IP de la cámara cambió) o WiFi.

## 7. Caso: «sin conexión» en el frontend

Casi siempre es el túnel/heartbeat, no la puerta/cámara (son locales):
```bash
journalctl -u frpc -n 50 --no-pager                  # túnel (si parpadea = WiFi inestable)
journalctl -u device_configurator -n 80 --no-pager
```
Un `reboot` suele restablecer un túnel que parpadea.

## 8. Dispositivos LEGACY (Raspberry Pi)

Su estado online/offline sale en la tabla legacy (ver §1 / `AGENTS.queries.md`).
Si está **offline**:
- En **puertas/tornos** suele ser que **el dueño lo apagó** (poco grave).
- En **cámaras** suele ser que **se ha caído**: el sistema legacy es inestable.
- Si la **Raspberry Pi está realmente rota**: es **legacy, no le damos soporte ni
  cambiamos piezas** (nadie sabe, está obsoleto). Hay que **convencer al dueño de
  cambiar de Raspberry Pi a ODROID** (el sistema nuevo). Señálalo en el diagnóstico.

## Notas

- `mqtt-sender` y `videorecorder` ya **no hacen polling**: usan `inotify`
  (`inotify_watch.py`), despiertan en ~1 ms. Fallback automático al polling. Sin
  dependencias (solo stdlib + libc).
- Deploy de código: `git pull` **manual** desde `municio1925/turnstile_controller`
  (rama `odroid`) + `sudo systemctl restart <servicio>`. No hay auto-pull.
- Recuerda la **regla de oro**: diagnostica y resume; no toques nada sin preguntar.
