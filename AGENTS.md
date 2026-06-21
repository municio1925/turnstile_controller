# AGENTS.md — Depurar dispositivos ODROID (turnstile_controller)

Guía breve para un agente (Claude Code / Codex) que tiene que diagnosticar un
dispositivo en una instalación. Cada gimnasio tiene una **puerta/torno** (lee el
QR y abre) y, si hay vídeo, una **cámara**. Todos los servicios son units de
`systemd` y el código vive en `~/turnstile_controller`.

## ⚡ Lo PRIMERO de todo: carga las credenciales

Antes de conectar a nada, carga el fichero **`.env.agents`** (en la raíz del
repo, **gitignored**) para tener las contraseñas como variables de entorno:

```bash
set -a; source .env.agents; set +a
```

**No** abras, `cat`, `grep` ni imprimas ese fichero ni sus valores — solo
súrcalo y usa las variables. Si **no existe**, créalo copiando
`.env.agents.example` y rellenándolo con el fichero de contraseñas (o pídele los
valores al usuario). Variables que provee:

- `DEVICE_SSH_PASSWORD` — contraseña de los ODROID/Raspberry (puertas, tornos,
  cámaras). **Es la que necesitas casi siempre.** Acompañada de `DEVICE_SSH_USER`
  (`manager`; algún ODROID usa `root`) y `DEVICE_SSH_HOST` (el relay FRP).
- `INFRASTRUCTURE_SSH_PASSWORD` + `BACKEND_SSH` / `FRONTEND_SSH` /
  `FRP_JUMPHOST_SSH` — solo si hay que depurar el turnstile **junto con el
  backend o el frontend** (mirar logs del servidor, etc.).

## 1. Conectarse por SSH

1. El comando SSH exacto (con su **puerto**) está en el **frontend**: **Control
   de Acceso → Dispositivos → uncollapse → «SSH remoto»**. Cada dispositivo tiene
   su puerto; el host es `$DEVICE_SSH_HOST` y el usuario `manager`.
2. `ssh` normal se queda esperando la contraseña y **el agente no puede
   teclearla**. Pásala con `sshpass` usando la variable ya cargada:
   ```bash
   sshpass -p "$DEVICE_SSH_PASSWORD" ssh -o StrictHostKeyChecking=no \
     -p 6020 "$DEVICE_SSH_USER@$DEVICE_SSH_HOST" 'cd ~/turnstile_controller && <comando>'
   ```
   Si falta `sshpass`: `sudo apt-get install -y sshpass` (o en mac
   `brew install hudochenkov/sshpass/sshpass`); alternativa: un script con `paramiko`.
3. Para `sudo` en el dispositivo (reiniciar un servicio) usa la misma contraseña
   por stdin:
   ```bash
   sshpass -p "$DEVICE_SSH_PASSWORD" ssh -p 6020 "$DEVICE_SSH_USER@$DEVICE_SSH_HOST" \
     "echo \"$DEVICE_SSH_PASSWORD\" | sudo -S systemctl restart mqtt-sender"
   ```
4. Para entrar a un **servidor** (backend / frontend / FRP) en lugar de a un
   dispositivo, usa `INFRASTRUCTURE_SSH_PASSWORD` con el destino correspondiente:
   ```bash
   sshpass -p "$INFRASTRUCTURE_SSH_PASSWORD" ssh -o StrictHostKeyChecking=no "$BACKEND_SSH" '<comando>'
   ```
5. La config del dispositivo (broker MQTT, cámara, relé, S3…) está en
   `~/turnstile_controller/.env`.

## 2. Ver logs

```bash
journalctl -u <servicio> -n 80 --no-pager      # últimas 80 líneas
journalctl -u <servicio> -f                     # en vivo (seguir)
journalctl -u <servicio> --since "10 min ago"   # ventana de tiempo
systemctl status <servicio>                      # ¿activo / fallido?
```

## 3. Mapa de servicios

| Dónde | Servicio | Para qué |
|-------|----------|----------|
| Puerta/torno | `qr_script_a` (y `qr_script_b` si hay 2 lectores) | lee el QR, valida y acciona el relé (abre) |
| Puerta/torno | `mqtt-sender` | al leer un QR, avisa a la cámara por MQTT para que grabe |
| Cámara | `mosquitto` | broker MQTT (recibe el aviso) |
| Cámara | `mqtt-receiver` | recibe el aviso y deja la señal de grabación |
| Cámara | `videorecorder` | graba ~6 s de vídeo |
| Cámara | `upload` | sube el `.mp4` a S3 |
| Ambos | `device_configurator` | heartbeat + comandos del backend (config, reboot) |
| Ambos | `frpc` | túnel al relay (lo que da el «SSH remoto» y el online/offline) |

## 4. Problema: la cámara NO graba (vídeo)

El flujo cruza **dos** dispositivos, así que necesitas SSH **a la cámara** y, a
ser posible, **también al dispositivo disparador** (puerta/torno). Cadena completa:

1. Disparador · `qr_script_a` lee el QR y deja un fichero de disparo en `RECORDING_DIR`.
2. Disparador · `mqtt-sender` ve el fichero y publica `[uuid, ts]` al broker de la cámara (`MQTT_BROKER` en `.env` = IP LAN de la cámara).
3. Cámara · `mosquitto` recibe el mensaje.
4. Cámara · `mqtt-receiver` escribe `record.txt` + `<uuid>.txt`.
5. Cámara · `videorecorder` graba y guarda `<uuid>.mp4`.
6. Cámara · `upload` lo sube a S3.

Qué revisar:
```bash
# En el disparador (puerta/torno):
journalctl -u qr_script_a -n 50 --no-pager           # ¿leyó el QR?
journalctl -u mqtt-sender -n 50 --no-pager           # ¿"Found trigger file" + "Sent payload"? ¿a qué broker?
grep MQTT_BROKER ~/turnstile_controller/.env          # ¿apunta a la IP correcta de la cámara?

# En la cámara:
journalctl -u mosquitto    -n 50 --no-pager
journalctl -u mqtt-receiver -n 50 --no-pager          # ¿"Received data for UUID"?
journalctl -u videorecorder -n 50 --no-pager          # ¿"Started recording"? ¿errores de cámara?
journalctl -u upload       -n 50 --no-pager           # ¿subió a S3?
```
Pistas: `mqtt-sender` envía pero `mqtt-receiver` no recibe → `MQTT_BROKER`
incorrecto (la IP de la cámara cambió) o WiFi inestable. Graba pero no aparece en
el panel → mira `upload`. `videorecorder` con errores de apertura → cámara USB.

## 5. Problema: la puerta (no abre, o revisar entradas/fallos del día)

Todo está en el lector de QR del disparador, `qr_script_a` (y `qr_script_b` si
hay dos lectores):
```bash
journalctl -u qr_script_a -n 80 --no-pager                  # últimas lecturas
journalctl -u qr_script_a --since today --no-pager          # actividad de HOY (entradas)
journalctl -u qr_script_a --since today --no-pager | grep -iE "error|fail|exception|warning"   # fallos de hoy
```
Si hay dos entradas/lectores, repite con `qr_script_b`. En la traza verás la
lectura del QR, la validación contra el backend y el accionamiento del relé. Para
contrastar con el servidor, entra al backend con `INFRASTRUCTURE_SSH_PASSWORD`.

## 6. Problema: el dispositivo aparece «sin conexión» en el frontend

Casi siempre es el túnel o el heartbeat, no la puerta/cámara (que son locales y
siguen funcionando):
```bash
journalctl -u frpc -n 50 --no-pager                  # túnel al relay (si parpadea = WiFi inestable)
journalctl -u device_configurator -n 80 --no-pager   # heartbeat / comandos del backend
```
Un `reboot` suele restablecer un túnel que está parpadeando.

## Notas

- `mqtt-sender` y `videorecorder` ya **no hacen polling**: usan `inotify`
  (`inotify_watch.py`) y despiertan en ~1 ms cuando aparece el fichero de
  disparo. Si `inotify` fallara, caen automáticamente al polling anterior. No
  hace falta instalar nada (solo stdlib + libc).
- Los dispositivos hacen `git pull` **manual** desde `municio1925/turnstile_controller`
  (rama `odroid`). No hay auto-pull: para desplegar hay que entrar por SSH y
  hacer `git pull` + `sudo systemctl restart <servicio>`.
