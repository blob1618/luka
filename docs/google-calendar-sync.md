# STK-252 · Sincronización de recordatorios con Google Calendar

## Activación

1. Aplicar la migración `20261031120000_stk252_google_calendar.sql` antes de desplegar el backend. Verificar su aplicación en remoto con `supabase migration list` y comprobar que existen las dos tablas y las dos columnas nuevas de `recordatorio`.
2. En Google Cloud, habilitar **Google Calendar API** y configurar la pantalla de consentimiento OAuth. Crear un cliente OAuth de tipo **Web application** con el scope `https://www.googleapis.com/auth/calendar.events.owned`. Si la app se ofrece públicamente, completar la verificación que Google solicite para ese scope.
3. Registrar como redirect URI exacto `https://<backend>/integrations/google-calendar/callback`.
4. Configurar en Render `GOOGLE_CALENDAR_CLIENT_ID`, `GOOGLE_CALENDAR_CLIENT_SECRET`, `GOOGLE_CALENDAR_REDIRECT_URI` y `GOOGLE_CALENDAR_FERNET_KEY`. Generar la clave una sola vez con el comando indicado en `.env.example`; conservarla entre despliegues. Perderla impide descifrar los permisos ya guardados y requiere reconectar a cada usuario.
5. Desplegar con un solo worker de scheduler, como indica el `Dockerfile`.

No se debe poner ninguna de estas credenciales en el frontend ni en el repositorio. El backend necesita HTTPS público para recibir el callback.

## Uso

El usuario registrado escribe **«conectar calendario»**, **«vincular calendario»** o **«sincronizar calendario»** por WhatsApp. Luka responde con un botón que lleva al consentimiento de Google. El enlace vence a los diez minutos, se usa una sola vez y solicita acceso offline únicamente a eventos de calendarios que el usuario posee. Tras aprobarlo, el callback confirma la conexión y lanza una primera sincronización.

Los recordatorios manuales y los inteligentes aceptados generan eventos mensuales de día completo en el calendario principal. El evento es privado y no bloquea la agenda. Se guarda su ID externo en `recordatorio.google_calendar_event_id`. El título y el monto, si existe, se envían a Google para que el compromiso sea legible en el calendario. No se envían otros movimientos financieros.

El día 29, 30 o 31 se ajusta al último día de los meses más cortos, como en Luka. La alerta nativa usa `dias_anticipacion`. Google limita esa alerta a 28 días; para recordatorios configurados con 29 o 30 días, el evento se mantiene pero la alerta de Google se fija en 28 días. El aviso de WhatsApp conserva su anticipación original.

## Fallos y verificación

El recordatorio interno siempre se confirma primero. Un job cada minuto compara su estado con la última versión enviada a Google. Los cambios de título, día, monto y anticipación actualizan el evento; pausar o eliminar borra el evento; reactivar lo crea de nuevo. Los fallos de Google dejan la versión pendiente para el siguiente ciclo. Los ID de evento se derivan de los UUID de Luka para que un reintento tras una caída no cree copias.

Los logs `[CALENDAR_AUTH]` y `[CALENDAR_SYNC]` registran tipos de error y latencia externa sin tokens ni datos financieros. Si los refresh tokens dejan de funcionar, el usuario debe solicitar un enlace nuevo y volver a autorizar. El job no llama a Google para recordatorios sin cambios.

La validación real pendiente de despliegue consiste en autorizar una cuenta de prueba, comprobar un evento inicial y verificar en Google Calendar las operaciones de editar, pausar, reactivar y eliminar desde WhatsApp. Probar una falla temporal de Google con un entorno aislado; no usar una cuenta de producción para provocar fallos.
