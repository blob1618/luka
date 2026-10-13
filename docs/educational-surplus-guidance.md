# Orientación educativa sobre excedentes

Luka puede responder «¿cuánto me sobró el mes pasado?» con una cifra basada
únicamente en los movimientos ya registrados del usuario, y ofrecer después
orientación educativa general sobre ahorro e inversión. La consulta es de solo
lectura: no registra ni modifica movimientos, categorías, límites,
presupuestos ni recordatorios, y no consulta al LLM. El código vive en
`app/services/educational_route.py` (clasificación y respuesta) y
`app/services/monthly_balance.py` (agregación).

## Cálculo y período predeterminado

- El período es el **último mes calendario cerrado** tomado como `[inicio, fin)`,
  con el fin exclusivo. Se calcula en `monthly_balance.previous_month_period`
  restando un día al primer día del mes en curso; eso cubre el cambio de año
  (enero → diciembre anterior) y los febreros bisiestos sin casos especiales.
- La referencia temporal es la fecha actual en `America/Argentina/Buenos_Aires`
  (`ARGENTINA_TZ`, `monthly_balance.argentina_now`), no la hora del servidor ni
  la del usuario. Una consulta hecha el 15/10/2026 en Argentina cubre
  `01/09/2026` a `30/09/2026` inclusive y se muestra como
  «01/09/2026 al 30/09/2026».
- Si el pedido menciona un período abierto o actual («este mes», «en lo que va
  del mes») o varios meses, la ruta de saldo no se activa: solo se calcula el
  último mes cerrado. En ese caso puede seguir respondiendo educación estática.
- Los movimientos se agregan con `Decimal` (nunca en punto flotante), sumando
  ingresos y egresos por separado. La diferencia es `ingresos - egresos`.

## Monedas

Cada moneda se informa por separado y **nunca se convierte ni se suma entre
monedas**. El resultado agrupa por `moneda` y `tipo`, y ordena alfabéticamente
por moneda. Por ejemplo, para un período con ARS y USD, la respuesta trae una
línea por moneda y evalúa el sobrante positivo de cada una.

## Filtrado de movimientos

La consulta incluye solo movimientos que cumplen **todas** estas condiciones:

- pertenecen al usuario resuelto por `public.usuario.whatsapp_id` (el teléfono
  entrante);
- tienen `anulado_en IS NULL` (los anulados quedan excluidos);
- tienen `fecha_movimiento` dentro de `[inicio, fin)` del último mes cerrado.

Por eso un movimiento anulado, uno fuera del período y uno de otro usuario
nunca afectan el total ni aparecen en la respuesta. La ruta **no** inventa
movimientos ni completa la falta de datos con cero.

## Comportamiento según el resultado

| Estado | Cuándo ocurre | Respuesta visible |
| --- | --- | --- |
| `ok` con diferencia positiva | Hay al menos una moneda con `diferencia > 0`. | «Hay un sobrante positivo registrado en al menos una moneda (sin convertir entre monedas).» y, por moneda, `diferencia N (sobrante positivo registrado)`. |
| `zero_difference` | Todas las monedas con movimientos dan diferencia `0`. | «No hay un sobrante positivo registrado en el período.» y `diferencia 0,00 (sin sobrante positivo registrado)`. |
| `ok` con diferencia negativa o cero en una moneda | La moneda no tiene sobrante positivo. | `diferencia -N (sin sobrante positivo registrado)`. |
| `no_data` | No hay movimientos activos del usuario en el período. | «No hay movimientos registrados en el período … Sin registros no puedo calcular un sobrante y no lo reemplazo por cero.» |
| `user_not_found` | El teléfono no corresponde a un usuario vinculado. | «No encuentro una cuenta vinculada a este número…». |
| `error` | La consulta falló. | «No pude calcular el período … en este momento. Intentá nuevamente en unos minutos.» |

En ningún estado de cero, déficit, ausencia de datos o error se afirma que
exista un sobrante positivo, ni se inventa un valor `0,00`. Cuando el resultado
es `ok` con sobrante positivo y el pedido no incorporó orientación general,
Luka agrega: «Si querés, puedo ampliar con orientación general sobre ahorro e
inversión (por ejemplo, reserva para imprevistos, plazo fijo o FCI).»

## Salvedad sobre los movimientos registrados

Toda respuesta de saldo cierra con esta aclaración literal:

> Estas cifras surgen únicamente de los movimientos registrados: pueden faltar
> movimientos no cargados o pagos futuros, y no demuestran que el dinero esté
> disponible.

El texto no promete dinero disponible, no reemplaza registros faltantes y no
proyecta pagos futuros.

## Alcance de la orientación estática

- La orientación general (ahorro e inversión, reserva para imprevistos, plazo
  fijo, FCI y comparaciones) sale del catálogo versionado
  `prompts/investment_guidance.v1.json`; es contenido educativo general, no una
  recomendación personal ni una oferta.
- No se ejecutan operaciones ni se completan tasas, cotizaciones o valores
  vigentes. Ante un tema no revisado, el catálogo declara el límite.
- El esquema actual **no tiene ninguna entidad de billetera** (`billetera` /
  `wallet`): no hay saldo de billetera que leer ni escribir. Una prueba
  estructural comprueba que `Base.metadata.tables` no define esa entidad.

## Fuentes oficiales y revisión

- BCRA — Educación financiera: https://www.bcra.gob.ar/que-es-la-educacion-financiera/
- BCRA — Plazos fijos: claves para invertir tu dinero: https://www.bcra.gob.ar/plazos-fijos-tipos-plazos-y-rentabilidad/
- CNV — Fondos Comunes de Inversión: https://www.cnv.gov.ar/SitioWeb/FondosComunesInversion
- CNV — Normas (TOC2013 actualizado): https://www.cnv.gov.ar/sitioWeb/Content/assets/files/TOC2013.pdf

Estas son las mismas fuentes del catálogo educativo versionado. Fecha de
revisión del contenido y de esta guía: **2026-10-13**. No incluye datos de
mercado, tasas ni valores actuales.

## Evidencia automatizada

Pruebas focales (runner aislado, sin red ni datos reales):

```bash
python -I .codex/dev.py test -q tests/test_educational_route.py tests/test_monthly_balance.py
```

En `tests/test_educational_route.py`:

- `test_acceptance_positive_surplus_is_read_only_and_isolated` (STK-244): caso
  integrado al dispatcher con ARS 100.000 de ingreso y ARS 30.000 de egreso en
  el mes cerrado, una fila anulada, una fuera del período, un movimiento de
  otro teléfono y una categoría con `LimiteCategoria`. Afirma el período exacto
  (`01/09/2026 al 30/09/2026`), el monto (`diferencia 70.000,00`), la etiqueta
  positiva, las dos cláusulas de la salvedad, la ausencia de llamada al LLM, la
  ausencia de escrituras y la igualdad de los snapshots de movimientos y
  límites antes/después. Los montos anulados, fuera de período, ajenos y del
  límite no aparecen en la respuesta.
- `test_current_schema_defines_no_wallet_entity`: comprueba que el esquema
  actual no define una tabla de billetera.
- Casos existentes que siguen cubriendo multimoneda, cero/negativo, ausencia
  de datos, error, usuario no encontrado, estado pendiente y onboarding
  (STK-241 / STK-243).

Resultado observado al implementar esta tarea: 111 pruebas focales aprobadas y
Ruff sin hallazgos. También se ejecutan `python -I .codex/dev.py lint` y
`git diff --check`; los resultados y el SHA exacto quedan registrados para la
revisión independiente.

## Escenarios manuales del simulador

> **Pendiente de ejecución manual.** Estos guiones describen qué probar y qué
> salida esperar; no se ejecutaron en el alcance de esta tarea y no deben
> presentarse como evidencia corrida.

El simulador Streamlit (`testing/`) es un **entorno de prueba separado** que se
levanta **únicamente con Docker/Podman** (`testing/docker-compose.yml`); no se
corre con `streamlit run` local. Usa una base SQLite aislada
(`TESTING_DATABASE_URL`), no toca `luka.db` ni Supabase, y requiere `.env` en la
raíz. Para cada escenario, escribir el mensaje en el chat como sesión vinculada
y comparar la respuesta del panel.

> **Atención — controles destructivos.** En la sidebar, desvincular la sesión
> (checkbox **Vinculado** en off) borra el usuario de prueba y sus movimientos,
> categorías y recordatorios; el botón **🔄 Reset DB** borra movimientos,
> categorías y recordatorios del usuario de la sesión activa. Usar siempre
> **datos sintéticos descartables**, nunca datos reales ni personales.

Escenarios (los importes deben registrarse con fecha dentro del **último mes
cerrado** real al momento de la prueba):

| # | Preparación (datos sintéticos) | Mensaje | Salida esperada |
| --- | --- | --- | --- |
| 1 | Ingreso ARS 100.000 y egreso ARS 30.000. | `¿Cuánto me sobró el mes pasado?` | «Hay un sobrante positivo registrado…», `diferencia 70.000,00 (sobrante positivo registrado)` y la salvedad completa. |
| 2 | Ingreso ARS 50.000 y egreso ARS 50.000. | `¿Cuánto me sobró el mes pasado?` | «No hay un sobrante positivo registrado en el período.» y `diferencia 0,00 (sin sobrante positivo registrado)`. |
| 3 | Ingreso ARS 20.000 y egreso ARS 35.000. | `¿Cuánto me sobró el mes pasado?` | `diferencia -15.000,00 (sin sobrante positivo registrado)`; sin afirmación de sobrante. |
| 4 | Sin movimientos en el mes cerrado. | `¿Cuánto me sobró el mes pasado?` | «No hay movimientos registrados en el período …»; sin `0,00`. |
| 5 | Ingreso y egreso en ARS, más ingreso y egreso en USD. | `¿Cuánto me sobró el mes pasado?` | Una línea por moneda, sin conversión ni suma entre ARS y USD. |
| 6 | Un movimiento anulado dentro del mes cerrado. | `¿Cuánto me sobró el mes pasado?` | El movimiento anulado no aparece ni altera los totales. |
| 7 | Dos sesiones con teléfonos distintos y montos distintos. | `¿Cuánto me sobró el mes pasado?` en cada sesión. | Cada sesión ve solo su propio monto; no hay fuga entre usuarios. |

Al terminar cada corrida, verificar que no cambió ningún movimiento ni límite
(el panel de debug muestra el servicio invocado `educational_route` y la ruta no
llama al LLM).
