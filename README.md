# Stealth Quest Completer

Bot de Discord que completa quests automáticamente usando el token del usuario.

## Cómo funciona

1. El usuario escribe `,autoquest` en el servidor donde está el bot
2. Si no tiene token guardado, el bot le envía un **DM** pidiendo su token de Discord
3. El usuario responde al DM con su token → se guarda en memoria (nunca en disco)
4. El usuario escribe `,autoquest` de nuevo en el servidor
5. El bot completa todas las quests disponibles automáticamente
6. El token se borra de memoria tras 1 hora o con `,clear`

## Seguridad del token

- 🔒 El token se pide por **DM** (mensaje privado)
- 🔒 Se guarda **solo en memoria** (RAM), nunca en disco ni logs
- 🔒 Se borra automáticamente tras 1 hora de inactividad
- 🔒 `,clear` lo borra inmediatamente
- 🔒 No se comparte con nadie, no se envía a ningún servidor externo

## Setup

### 1. Crear el bot en Discord

1. Ve a https://discord.com/developers/applications
2. Click en "New Application" → ponle un nombre
3. Ve a "Bot" → copia el token
4. Activa: **Message Content Intent**, **Server Members Intent**, **Presence Intent**
5. Ve a "OAuth2 → URL Generator" → selecciona `bot` + `applications.commands`
6. Permisos: Send Messages, Read Message History, Embed Links
7. Invita el bot a tu servidor con la URL generada

### 2. Instalar y ejecutar

```bash
git clone https://github.com/requiemzc/stealth-quest-completer.git
cd stealth-quest-completer
pip install -r requirements.txt
```

Copia `.env.example` a `.env` y pon tu bot token:
```bash
cp .env.example .env
# Edita .env y pon tu token
```

Ejecuta:
```bash
python bot.py
```

### 3. Hospedar 24/7

Puedes hospedar el bot en:
- **VPS** (contabo, hetzner, digitalocean) — más barato
- **Raspberry Pi** en casa
- **Railway.app** (free tier)
- **Render.com** (free tier)

## Comandos

| Comando | Descripción | Dónde |
|---|---|---|
| `,autoquest` | Completa todas tus quests (envía DM si no tienes token) | Servidor |
| `,quests` | Lista tus quests pendientes | Servidor |
| `,status` | Muestra si tienes token guardado | Servidor |
| `,clear` | Borra tu token de la memoria | Servidor o DM |
| `,help` | Muestra los comandos | Servidor |

## Flujo de uso

```
Usuario: ,autoquest
Bot: 📩 Te he enviado un DM para introducir tu token.

(DM del bot)
Bot: 🔒 Introduce tu token de Discord...
Usuario: [pega su token]
Bot: ✅ Token guardado en memoria. Ve al servidor y escribe ,autoquest.

(Servidor)
Usuario: ,autoquest
Bot: 🚀 Iniciando completion de quests...
Bot: ✅ Conectado como usuario123
Bot: 🎯 Encontradas 3 quests. Completando...
Bot: ✅ Quest 1 — completada!
Bot: ✅ Quest 2 — completada!
Bot: ✅ Quest 3 — completada!
Bot: 📊 Resumen: ✅ 3 completadas • ⏭️ 0 ya hechas • ❌ 0 fallidas
```

## ⚠️ Disclaimer

Discord ha estado enforcing contra quest automation desde abril 2026. El uso de esta herramienta puede resultar en restricciones de quests (hasta 14 días) o violaciones de Account Standing en tu cuenta. Úsalo bajo tu propio riesgo.

## Licencia

MIT
