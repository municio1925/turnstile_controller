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
- **«Online» se mide DISTINTO según la tabla — no uses el mismo umbral para las dos:**
  - **ODROID** (`fitnessmanager_api_device.last_seen_at`): es heartbeat real
    (frpc/`device_configurator`). Aquí sí: `last_seen_at` viejo (>15–30 min) =
    **caído** aunque el flag diga «online».
  - **Legacy** (`env_files.last_success_at` / `status`): **NO es heartbeat**. Se
    refresca solo **2 veces al día** (12:00 y 16:00 hora España) con un sondeo SSH
    desde el servidor (ver §8). Un `last_success_at` de hasta **~20 h** es **normal**
    (el hueco nocturno entre el sync de las 16:00 y el de las 12:00) y **NO** significa
    caído. Para saber si un legacy está vivo AHORA, **entra por SSH** (la verdad) o
    **dispara el sync** manualmente (§8); no te fíes solo de la antigüedad.
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
- Envía pero no recibe → `MQTT_BROKER` mal (la IP de la cámara cambió), WiFi, o el
  **broker de la cámara no acepta LAN**: en la cámara `mosquitto` debe escuchar en
  `0.0.0.0:1883` (con `lan-listener.conf` en `/etc/mosquitto/conf.d/`); si solo
  escucha en `127.0.0.1`, el disparador no puede publicarle.

**OJO — una cámara SIEMPRE figura como «sin vincular» (unpaired) en el frontend; es
NORMAL, no es avería.** Una cámara no tiene entrada propia (graba la entrada del
disparador), así que nunca recibe el estado «vinculado»; el enlace real vive en el
**disparador** (torno/puerta), que tiene la cámara asignada y su `MQTT_BROKER`
apuntando a la IP de la cámara. No te fíes del estado «vinculado/sin vincular»:
confirma con una **prueba de extremo a extremo** — deja un disparo en el disparador
y comprueba que la cámara graba:
```bash
# en el DISPARADOR (torno/puerta). RECORDING_DIR sale del .env (p.ej. ~/turnstile_controller/camera):
U=$(python3 -c "import uuid;print(uuid.uuid4())"); touch "$RECORDING_DIR/$U.txt"
# en la CÁMARA: ¿recibió y grabó?
journalctl -u mqtt-receiver --since "30 sec ago" --no-pager | grep "$U"   # -> "Received data for UUID"
ls -la "$RECORDING_DIR/$U.mp4"                                            # -> debe existir un .mp4
```
(Borra luego el `.mp4` de prueba: el servicio `upload` lo subiría a S3 como basura.)

## 7. Caso: «sin conexión» en el frontend

Casi siempre es el túnel/heartbeat, no la puerta/cámara (son locales):
```bash
journalctl -u frpc -n 50 --no-pager                  # túnel (si parpadea = WiFi inestable)
journalctl -u device_configurator -n 80 --no-pager
```
Un `reboot` suele restablecer un túnel que parpadea.

## 8. Dispositivos LEGACY (Raspberry Pi)

Su estado online/offline sale en la tabla legacy (ver §1 / `AGENTS.queries.md`).

**Cómo se llena `env_files` (clave para no malinterpretar el «offline»):** lo
actualiza la tarea Celery `task_sync_env_files` (`fitnessmanager_api.tasks`),
agendada en django_celery_beat como `sync_env_files_noon` (**12:00**) y
`sync_env_files_afternoon` (**16:00**, hora España, tz `Europe/Madrid`). En cada
pasada coge la lista de dispositivos de **la propia tabla `env_files`** (`name` +
`port` + `ssh_command`; Notion quedó retirado), hace **SSH a cada uno** para leer
su `.env` y escribe `status` (`SUCCESS` si conectó / `ERROR` si no) y `last_success_at`
(solo al conectar). O sea: `env_files` refleja **si el último sondeo de las 12:00/16:00
pudo entrar por SSH**, no el estado en tiempo real → un dispositivo «atrasado ~20 h»
de madrugada/mañana es **esperado, no avería**. (Esto NO es un fallo de Celery beat:
beat está vivo y la tarea tarda ~10 s; «N failed» son equipos caídos o plantillas.)
- Los puertos **`*Master`** (`tornoRaspberryMaster`, `odroidMaster`, `cameraMaster`)
  salen **siempre** en `ERROR`: son tarjetas SD plantilla, **no clientes** → ignóralos.
- **Para forzar una lectura fresca on-demand** (en el backend; es de solo lectura,
  solo hace SSH + upsert de estado):
  ```bash
  docker exec fitnessmanager-web-1 python manage.py shell -c \
    "from fitnessmanager_api.tasks import task_sync_env_files; print(task_sync_env_files.delay().id)"
  docker logs --since 3m fitnessmanager-celery_worker-1   # «[puerto] alias: OK/FAILED» + «Done: N success, M failed»
  ```
  Si quieres saber YA si un legacy está vivo, esto (o un SSH directo) manda; la
  antigüedad de `env_files` no.

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
- **Actualizar/migrar los ODROID de un gimnasio** (pasar a municio, traer el último
  código, completar sudoers, reiniciar servicios sin tirar la puerta, casos
  especiales root/Pi, estado por gym — Galaxy pendiente/offline): ver
  `AGENTS.update.md` (fichero local, gitignored, fuera del repo público).
- Recuerda la **regla de oro**: diagnostica y resume; no toques nada sin preguntar.
