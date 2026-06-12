# Clonador de tarjetas Odroid

Aplicacion grafica nativa para Ubuntu, pensada para:

- clonar una micro SD origen a una micro SD destino
- reservar un puerto FRP unico en el backend
- grabar ese puerto en la tarjeta destino
- configurar una tarjeta ya clonada sin volver a clonar

## Dependencias en la maquina que compila

- `gcc`
- `gtk+-3.0` de desarrollo

## Dependencias en la maquina que ejecuta

No necesita Flutter ni Java.

Necesita un Ubuntu normal con estas herramientas del sistema:

- `pkexec`
- `lsblk`
- `dd`
- `curl`

## Compilar

```bash
cd odroid_clone
make
```

## Preparar una carpeta para entregar

```bash
cd odroid_clone
make package
```

Esto crea:

- `dist/clonador_sd`
- `dist/odroid_sd_helper.sh`

Entrega ambos archivos juntos en la misma carpeta.

## Ejecutar

```bash
cd dist
./clonador_sd
```

El helper `odroid_sd_helper.sh` debe quedarse al lado del binario.
