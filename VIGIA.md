# Vigía de convocatorias — puesta en marcha

El sistema encontraba buenas oportunidades, pero solo cuando alguien se acordaba de abrirlo:
11 sesiones en tres meses, ninguna desde julio. El vigía invierte eso. Corre solo una vez por
semana, busca sobre los temas de interés, descarta lo que ya reportó antes y manda un correo
con lo nuevo. El trabajo pasa de **buscar** a **decidir**.

## Qué hace exactamente

1. Lee los temas de `vigia_temas.txt` (o de `VIGIA_TEMAS`).
2. Por cada tema ejecuta el scouting completo: búsqueda web dirigida a los sitios oficiales
   de los financiadores y a los portales ecuatorianos, calificación ponderada sobre 100,
   verificación de que Ecuador es elegible, de que la URL responde y de que la fecha de cierre
   —re-descargada de la página oficial— sigue vigente.
3. Descarta lo que ya salió en un reporte anterior (`output/vigia_historial.json`, con caducidad
   de 120 días para que una convocatoria de ciclo anual vuelva a aparecer cuando se reabre).
4. Ordena todo por calificación, se queda con las mejores y envía un solo correo.
5. Cada corrida queda registrada como sesión, así que el correo puede enlazar al panel, y desde
   ahí se lanza la redacción de la propuesta con el pipeline que ya existe.

Solo entran convocatorias con **≥ 60/100**. Si no hay nada nuevo, no manda correo: el silencio
significa que buscó y no había nada, no que se rompió.

## Qué entra en el correo y qué no

La calificación mide **encaje**, no **existencia**. En la primera corrida real, el agente devolvió
71/100 sobre una nota de prensa del PNUD sobre políticas de IA: un proyecto perfectamente
razonable, pero sin convocatoria detrás. Por eso hay un segundo filtro, independiente del score:

- **Cuerpo del correo**: solo lo que tiene **fecha de cierre verificada y vigente**. Una
  convocatoria real publica cuándo cierra; si no hay fecha, no hay nada que decidir.
- **Pistas, al final y en una línea**: lo que puntuó alto pero no tiene fecha. Son rastros que
  pueden acabar en una convocatoria, no oportunidades. No se registran en el historial, así que
  si una madura y aparece con fecha, entra al cuerpo del correo como novedad.
- **Si solo hay pistas, no se envía nada.** La promesa es que un correo del vigía siempre trae
  algo sobre lo que decidir; en cuanto trae relleno, deja de leerse.

## Configuración del correo (2 minutos, una sola vez)

Se usa SMTP con una cuenta de Gmail que ya existe. No hace falta verificar un dominio ni
esperar la aprobación de ningún proveedor.

1. En la cuenta de Google, con la verificación en dos pasos activada, cree una
   **contraseña de aplicación** (Seguridad → Contraseñas de aplicaciones). Son 16 caracteres.
2. Configure estas variables donde vaya a correr el vigía:

```
VIGIA_SMTP_USER=jomapconsultores@gmail.com
VIGIA_SMTP_PASS=<la contraseña de aplicación, sin espacios>
VIGIA_MAIL_TO=jomapconsultores@gmail.com
VIGIA_PANEL_URL=https://proyectos.pensamiento-libre.org
VIGIA_HISTORIAL=/app/output/vigia_historial.json
```

3. Compruebe el canal antes de programar nada:

```
python -m utils.mailer          # dice si está configurado y manda un correo de prueba
```

`VIGIA_SMTP_PASS` es una contraseña de aplicación, **nunca** la contraseña de la cuenta: se
puede revocar sola si se filtra, y no da acceso al correo.

## Comprobación antes de automatizar

```
python vigia.py --dry-run --tema "capacitación y formación profesional"
```

Busca de verdad (tarda varios minutos y consume llamadas a los modelos), muestra el resultado
por pantalla y **no** envía ni registra nada en el historial.

## Tarea programada semanal

El repositorio ya está desplegado en el servidor dentro del contenedor cuyo nombre empieza por
`wdq1nb7id7ihcdoti1kytpxy` (Coolify le cambia el sufijo en cada despliegue, por eso se resuelve
por prefijo y no por nombre completo).

Cree `/opt/vigia/vigia.env` con las cuatro variables de arriba y permisos `600`, y
`/opt/vigia/correr.sh`:

```sh
#!/bin/sh
# Vigía de convocatorias: una corrida semanal. El contenedor se resuelve por el
# prefijo del recurso porque Coolify le añade un sufijo distinto en cada despliegue.
c=$(docker ps -q --filter name=^wdq1nb7id7ihcdoti1kytpxy | head -1)
[ -n "$c" ] || { echo "$(date -Is) contenedor de proyectos no encontrado"; exit 1; }

# El historial vive en el HOST y se presta al contenedor. Sin esto, cada
# redespliegue de Coolify recrearía el contenedor con `output/` vacío y el vigía
# volvería a reportar como nuevas las convocatorias ya enviadas.
mkdir -p /opt/vigia/estado
[ -f /opt/vigia/estado/vigia_historial.json ] &&
  docker cp /opt/vigia/estado/vigia_historial.json "$c":/app/output/vigia_historial.json

docker exec --env-file /opt/vigia/vigia.env "$c" python vigia.py
rc=$?

docker cp "$c":/app/output/vigia_historial.json /opt/vigia/estado/vigia_historial.json 2>/dev/null
exit $rc
```

Y en el crontab, siguiendo la convención de las demás tareas del servidor:

```
# Vigía de convocatorias: busca oportunidades y las manda por correo. Lunes 07:00.
0 7 * * 1 /opt/vigia/correr.sh >> /var/log/vigia.log 2>&1
```

## Códigos de salida

| Código | Significado |
|---|---|
| 0 | Corrida correcta (con correo enviado, o sin novedades que reportar) |
| 2 | Encontró oportunidades pero **no pudo enviar**: el reporte quedó en `output/vigia/`. Revise el correo |
| 1 | Error de ejecución |

El código 2 existe para que una tarea sin correo configurado se note en el log en vez de pasar
por buena semana tras semana.

## De dónde sale el criterio

El perfil que decide si una convocatoria encaja es `perfil-map/PERFIL_MAP.md` — entidades,
sectores y diferenciadores. Al editarlo, ejecute `python -m utils.perfil` para refrescar la
copia que viaja dentro del repositorio; sin eso, el contenedor sigue usando la versión anterior.
