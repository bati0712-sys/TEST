# Despliegue del RedBF Agent

## Compilar (en tu PC de desarrollo)

```
cd agente
build.bat          # genera dist\redbf-agent.exe (requiere: pip install pyinstaller)
empaquetar.bat     # arma installer\ + RedBF-Agent-Installer.zip
```

El ZIP `RedBF-Agent-Installer.zip` contiene todo lo necesario:
`redbf-agent.exe`, `nssm.exe`, `config.ini`, `instalar.bat`, `desinstalar.bat`.

## Instalar en una PC de la red

1. Copiar `RedBF-Agent-Installer.zip` a la PC y descomprimir en una carpeta fija
   (ej. `C:\RedBF-Agent\`).
2. Editar **`config.ini`**:
   - `server_url` = IP del servidor RedBF en la LAN (ej. `http://192.168.1.71:8077`)
   - `token` = mismo valor que `REDBF_TOKEN` del servidor
   - `incluir_navegacion` = `false` si no quieres el resumen web en esa PC
3. Click derecho en **`instalar.bat`** → **Ejecutar como administrador**.

El agente queda como servicio Windows **RedBFAgent** (auto-arranque, auto-restart,
logs con rotación en `agente.log`). El equipo aparece en el dashboard en ~1 minuto.

## Comandos útiles (en la PC con el agente)

```
nssm status RedBFAgent       # estado del servicio
nssm restart RedBFAgent      # reiniciar
nssm stop RedBFAgent         # detener
powershell Get-Content agente.log -Wait -Tail 50   # logs en vivo
```

## Desinstalar

Click derecho en **`desinstalar.bat`** → Ejecutar como administrador.
(Detiene y remueve el servicio + quita exclusiones de Defender.)

## Notas

- **Solo LAN.** El agente reporta por HTTP al servidor interno; no expone nada a internet.
- El instalador **excluye la carpeta de Windows Defender** (evita que el .exe sea
  borrado por falso positivo, como pasa con cualquier agente de gestión).
- Probado: el `.exe` recolecta y reporta idéntico al script (hardware, 340 programas,
  navegación, red con DHCP/estática, antivirus incl. Acronis Cyber Protect).
- Para actualizar el agente: recompilar, reemplazar el `.exe` en la carpeta y
  `nssm restart RedBFAgent` (o re-correr `instalar.bat`).

## Captura de pantalla desde servicio (Session 0)

Un servicio Windows corre en Session 0 (aislada) y no ve el escritorio del
usuario. RedBF resuelve esto como NetSupport: cuando el agente (servicio) recibe
un comando de captura y detecta que está en Session 0, lanza una copia de sí
mismo **en la sesión interactiva del usuario** (`redbf-agent.exe --captura <archivo>`)
vía WTSQueryUserToken + CreateProcessAsUserW, lee la imagen y la reporta.

- Funciona solo si hay un usuario logueado en la consola (sesión activa).
- Si la PC está en la pantalla de login (sin sesión), la captura devuelve error
  "Helper en sesión de usuario falló" — esperado.
- Requiere que el servicio corra como LocalSystem (privilegio para CreateProcessAsUser).
  El instalar.bat ya lo configura así (ObjectName LocalSystem).
