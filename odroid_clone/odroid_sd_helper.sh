#!/usr/bin/env bash

set -euo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

wait_for_partition_table() {
  local dev="$1"
  partprobe "$dev" 2>/dev/null || true
  udevadm settle 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if lsblk -lnpo NAME,TYPE "$dev" | awk '$2=="part"{found=1} END{exit found?0:1}'; then
      return 0
    fi
    sleep 1
    partprobe "$dev" 2>/dev/null || true
    udevadm settle 2>/dev/null || true
  done
  return 1
}

unmount_partitions() {
  local dev="$1"
  lsblk -lnpo NAME,TYPE "$dev" | awk '$2=="part"{print $1}' | while IFS= read -r part; do
    umount "$part" 2>/dev/null || true
  done
}

resolve_disk() {
  local target="$1"
  [[ -b "$target" ]] || die "No existe el dispositivo: $target"

  local type
  type="$(lsblk -dn -o TYPE "$target" 2>/dev/null | awk 'NR==1{print $1}')"
  case "$type" in
    disk)
      printf '%s\n' "$target"
      return 0
      ;;
    part)
      local parent
      parent="$(lsblk -no PKNAME "$target" 2>/dev/null | head -n1)"
      [[ -n "$parent" ]] || die "No se pudo resolver el disco padre de: $target"
      printf '/dev/%s\n' "$parent"
      return 0
      ;;
    *)
      die "El dispositivo no es un disco ni una particion valida: $target"
      ;;
  esac
}

resolve_linux_partition() {
  local target="$1"
  [[ -b "$target" ]] || die "No existe el dispositivo: $target"

  local type
  type="$(lsblk -dn -o TYPE "$target" 2>/dev/null | awk 'NR==1{print $1}')"

  if [[ "$type" == "part" ]]; then
    printf '%s\n' "$target"
    return 0
  fi

  if [[ "$type" != "disk" ]]; then
    die "El dispositivo no es un disco ni una particion valida: $target"
  fi

  wait_for_partition_table "$target" || die "No se encontraron particiones en el dispositivo: $target"

  local best_partition=""
  local best_size=0
  while IFS='|' read -r part_path part_size part_fstype; do
    [[ -n "$part_path" ]] || continue
    case "$part_fstype" in
      ext4|ext3|ext2|btrfs|xfs)
        if [[ "${part_size:-0}" -gt "$best_size" ]]; then
          best_partition="$part_path"
          best_size="$part_size"
        fi
        ;;
    esac
  done < <(lsblk -bnrpo NAME,SIZE,FSTYPE,TYPE "$target" | awk '$4=="part"{printf "%s|%s|%s\n",$1,$2,$3}')

  if [[ -n "$best_partition" ]]; then
    printf '%s\n' "$best_partition"
    return 0
  fi

  best_partition="$(lsblk -lnpo NAME,TYPE "$target" | awk '$2=="part"{last=$1} END{print last}')"
  [[ -n "$best_partition" ]] || die "No se encontro ninguna particion utilizable en: $target"
  printf '%s\n' "$best_partition"
}

patch_frpc_ini() {
  local file="$1"
  local port="$2"
  [[ -f "$file" ]] || return 1

  if grep -qE '^[[:space:]]*\[ssh_[0-9]+\][[:space:]]*$' "$file"; then
    sed -E -i "0,/^[[:space:]]*\\[ssh_[0-9]+\\][[:space:]]*$/s//[ssh_${port}]/" "$file"
  fi

  if grep -qE '^[[:space:]]*remote_port[[:space:]]*=' "$file"; then
    sed -E -i "s#^([[:space:]]*remote_port[[:space:]]*=[[:space:]]*).*\$#\1${port}#" "$file"
  else
    printf '\nremote_port = %s\n' "$port" >>"$file"
  fi
  return 0
}

patch_frpc_toml() {
  local file="$1"
  local port="$2"
  [[ -f "$file" ]] || return 1

  if grep -qE '^[[:space:]]*remotePort[[:space:]]*=' "$file"; then
    sed -E -i "s#^([[:space:]]*remotePort[[:space:]]*=[[:space:]]*).*\$#\1${port}#" "$file"
  else
    printf '\nremotePort = %s\n' "$port" >>"$file"
  fi
  return 0
}

read_current_port() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  grep -E '^[[:space:]]*remote_port[[:space:]]*=' "$file" | tail -n1 | sed -E 's/^[^=]*=[[:space:]]*//'
}

read_current_port_toml() {
  local file="$1"
  [[ -f "$file" ]] || return 1
  grep -E '^[[:space:]]*remotePort[[:space:]]*=' "$file" | tail -n1 | sed -E 's/^[^=]*=[[:space:]]*//'
}

device_identifier_for_port() {
  local port="$1"
  printf 'odroid-%s\n' "$port"
}

write_device_identifier_env() {
  local env_file="$1"
  local port="$2"
  [[ -f "$env_file" ]] || return 1

  python3 - "$env_file" "$port" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
port = sys.argv[2]
device_identifier = f"odroid-{port}"

lines = env_path.read_text().splitlines()
out = []
written = False

for line in lines:
    if "=" not in line:
        out.append(line)
        continue
    key, _ = line.split("=", 1)
    if key == "DEVICE_IDENTIFIER":
        out.append(f'DEVICE_IDENTIFIER="{device_identifier}"')
        written = True
    else:
        out.append(line)

if not written:
    out.append(f'DEVICE_IDENTIFIER="{device_identifier}"')

env_path.write_text("\n".join(out) + "\n")
print(f"DEVICE_IDENTIFIER grabado: {device_identifier}")
PY
}

prepare_pairing_env() {
  local env_file="$1"
  [[ -f "$env_file" ]] || return 1

  python3 - "$env_file" <<'PY'
from pathlib import Path
import sys

env_path = Path(sys.argv[1])
keys_to_clear = {
    "GYM_UUID",
    "ENTRANCE_UUID",
    "ENTRANCE_UUID_A",
    "ENTRANCE_UUID_B",
    "ENTRANCE_DIRECTION",
    "ENTRANCE_DIRECTION_A",
    "ENTRANCE_DIRECTION_B",
}

lines = env_path.read_text().splitlines()
out = []
seen = set()
for line in lines:
    if "=" not in line:
        out.append(line)
        continue
    key, _ = line.split("=", 1)
    if key in keys_to_clear:
        out.append(f'{key}=""')
        seen.add(key)
    else:
        out.append(line)

for key in sorted(keys_to_clear - seen):
    out.append(f'{key}=""')

env_path.write_text("\n".join(out) + "\n")
print("Se limpiaron los campos de emparejamiento:")
for key in sorted(keys_to_clear):
    print(f" - {key}")
PY
}

inspect_port() {
  local target="$1"
  local partition
  partition="$(resolve_linux_partition "$target")"
  local mount_dir
  mount_dir="$(mktemp -d /tmp/odroid-cloner.XXXXXX)"
  trap "umount '$mount_dir' 2>/dev/null || true; rmdir '$mount_dir' 2>/dev/null || true" EXIT

  mount "$partition" "$mount_dir"

  local port=""
  local file=""
  if port="$(read_current_port "$mount_dir/etc/frpc.ini" 2>/dev/null)"; then
    file="/etc/frpc.ini"
  elif port="$(read_current_port_toml "$mount_dir/etc/frpc.toml" 2>/dev/null)"; then
    file="/etc/frpc.toml"
  fi

  if [[ -n "$file" ]]; then
    printf 'PARTITION=%s\nFILE=%s\nPORT=%s\n' "$partition" "$file" "$port"
  else
    printf 'PARTITION=%s\nFILE=\nPORT=\n' "$partition"
  fi
  umount "$mount_dir" 2>/dev/null || true
  rmdir "$mount_dir" 2>/dev/null || true
  trap - EXIT
}

configure_port() {
  local target="$1"
  local port="$2"
  local partition
  partition="$(resolve_linux_partition "$target")"
  local mount_dir
  mount_dir="$(mktemp -d /tmp/odroid-cloner.XXXXXX)"
  trap "sync || true; umount '$mount_dir' 2>/dev/null || true; rmdir '$mount_dir' 2>/dev/null || true" EXIT

  mount "$partition" "$mount_dir"

  local current_port=""
  local current_file=""
  if current_port="$(read_current_port "$mount_dir/etc/frpc.ini" 2>/dev/null)"; then
    current_file="/etc/frpc.ini"
  elif current_port="$(read_current_port_toml "$mount_dir/etc/frpc.toml" 2>/dev/null)"; then
    current_file="/etc/frpc.toml"
  fi

  if [[ -n "$current_file" ]]; then
    printf 'Puerto actual detectado en %s: %s\n' "$current_file" "$current_port"
  else
    printf 'No se detecto un puerto actual. Se escribira uno nuevo.\n'
  fi

  local patched=false
  patch_frpc_ini "$mount_dir/etc/frpc.ini" "$port" && patched=true || true
  patch_frpc_toml "$mount_dir/etc/frpc.toml" "$port" && patched=true || true

  if [[ "$patched" != true ]]; then
    die "No se encontro /etc/frpc.ini ni /etc/frpc.toml en la tarjeta."
  fi

  local env_file="$mount_dir/home/manager/turnstile_controller/.env"
  [[ -f "$env_file" ]] || die "No se encontro el archivo .env del controlador en la tarjeta."
  write_device_identifier_env "$env_file" "$port" || die "No se pudo grabar DEVICE_IDENTIFIER en la tarjeta."

  printf 'Puerto FRP e identificador de tarjeta grabados en la particion %s: puerto=%s identificador=%s\n' \
    "$partition" "$port" "$(device_identifier_for_port "$port")"
  sync || true
  umount "$mount_dir" 2>/dev/null || true
  rmdir "$mount_dir" 2>/dev/null || true
  trap - EXIT
}

prepare_pairing_state() {
  local target="$1"
  local port="$2"
  local partition
  partition="$(resolve_linux_partition "$target")"
  local mount_dir
  mount_dir="$(mktemp -d /tmp/odroid-cloner.XXXXXX)"
  trap "sync || true; umount '$mount_dir' 2>/dev/null || true; rmdir '$mount_dir' 2>/dev/null || true" EXIT

  mount "$partition" "$mount_dir"

  local env_file="$mount_dir/home/manager/turnstile_controller/.env"
  [[ -f "$env_file" ]] || die "No se encontro el archivo .env del controlador en la tarjeta."

  prepare_pairing_env "$env_file" || die "No se pudo preparar la tarjeta para emparejar."
  write_device_identifier_env "$env_file" "$port" || die "No se pudo grabar DEVICE_IDENTIFIER en la tarjeta."

  printf 'Tarjeta preparada para emparejar desde la particion %s con identificador=%s\n' \
    "$partition" "$(device_identifier_for_port "$port")"
  sync || true
  umount "$mount_dir" 2>/dev/null || true
  rmdir "$mount_dir" 2>/dev/null || true
  trap - EXIT
}

clone_disk() {
  local source="$1"
  local target="$2"

  [[ -b "$source" ]] || die "No existe el dispositivo origen: $source"
  [[ -b "$target" ]] || die "No existe el dispositivo destino: $target"
  [[ "$source" != "$target" ]] || die "El origen y el destino no pueden ser el mismo dispositivo."

  unmount_partitions "$source"
  unmount_partitions "$target"

  dd if="$source" of="$target" bs=16M status=progress conv=fsync
  sync
  wait_for_partition_table "$target" || true
}

eject_card() {
  local target="$1"
  local disk
  disk="$(resolve_disk "$target")"

  sync || true
  unmount_partitions "$disk"
  udevadm settle 2>/dev/null || true

  if command -v udisksctl >/dev/null 2>&1; then
    if udisksctl power-off -b "$disk" >/dev/null 2>&1; then
      printf 'Tarjeta expulsada correctamente: %s\n' "$disk"
      return 0
    fi
  fi

  if command -v eject >/dev/null 2>&1; then
    if eject "$disk" >/dev/null 2>&1; then
      printf 'Tarjeta expulsada correctamente: %s\n' "$disk"
      return 0
    fi
  fi

  printf 'Tarjeta desmontada correctamente: %s\n' "$disk"
}

main() {
  local command="${1:-}"
  shift || true

  case "$command" in
    clone)
      [[ $# -eq 2 ]] || die "Uso: clone <origen> <destino>"
      clone_disk "$1" "$2"
      ;;
    configure-port)
      [[ $# -eq 2 ]] || die "Uso: configure-port <disco-o-particion> <puerto>"
      configure_port "$1" "$2"
      ;;
    inspect-port)
      [[ $# -eq 1 ]] || die "Uso: inspect-port <disco-o-particion>"
      inspect_port "$1"
      ;;
    eject-card)
      [[ $# -eq 1 ]] || die "Uso: eject-card <disco-o-particion>"
      eject_card "$1"
      ;;
    prepare-pairing)
      [[ $# -eq 2 ]] || die "Uso: prepare-pairing <disco-o-particion> <puerto>"
      prepare_pairing_state "$1" "$2"
      ;;
    *)
      die "Comando no soportado: $command"
      ;;
  esac
}

main "$@"
