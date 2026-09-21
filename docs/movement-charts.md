# Gráficos financieros en WhatsApp

## Pedidos admitidos

Luka genera gráficos solo ante una solicitud explícita o una modificación explícita
de un gráfico reciente. El tipo predeterminado es barras, el alcance son egresos y el
período es el mes actual en `America/Argentina/Buenos_Aires`.

- Distribución por categoría: mayores o menores importes, ranking de 1 a 5 categorías
  más Otros; hasta seis elementos, preservando el total del conjunto filtrado.
- Filtros por una o varias categorías existentes y por moneda. Las coincidencias
  ambiguas o inexistentes se aclaran; no crean categorías. Sin categoría es un filtro
  válido para movimientos sin categoría asignada.
- Comparación de ingresos y gastos: ambos tipos en la misma escala, con saldo
  (ingresos menos gastos). Una serie sin registros muestra cero con una aclaración.
- Evolución mes a mes desde un mes inicial hasta el mes actual, o hasta un mes final
  explícito. Incluye meses vacíos y cambios de año; hasta 24 meses por pedido.
- Comparación de exactamente dos meses, incluso de años diferentes. Los meses se
  ordenan cronológicamente y la variación toma como base el más antiguo. Con base
  cero, la variación porcentual se indica como no calculable. No se comparan semanas
  ni días. No se incluyen los meses intermedios si no fueron seleccionados.
- El mes actual se consulta hasta hoy en los gráficos mensuales y se marca parcial.
  Los meses futuros se rechazan para solicitar una corrección.

Ejemplos:

1. `Mostrame las tres categorías con mayor gasto en un gráfico con porcentajes`.
2. `Que sea un gráfico pastel`.
3. `Ahora solo ocio y comida, en pesos`.
4. `Gráfico de egresos comparados con ingresos para ocio`.
5. `Mostrame un gráfico de gastos mes a mes desde enero de 2026`.
6. `Compará en un gráfico agosto y septiembre de 2026`.
7. `Compará enero de 2025 con enero de 2026 en un gráfico de ingresos`.

## Aclaraciones y contexto

El último gráfico generado guarda su especificación, no imágenes ni importes,
en Redis durante 30 minutos por usuario. Las modificaciones conservan los filtros
no mencionados; el reinicio de conversación elimina también esta referencia.
Sin contexto reciente se pide una nueva especificación. Los registros de movimientos,
cambios de límites y consultas de texto no se convierten en gráficos.

Cuando una comparación se solicita en pastel se ofrecen opciones numeradas y
concretas: mantener la comparación en barras, ver una distribución de gastos o
de ingresos; para pedidos mensuales se nombran los meses de las opciones. Responder
con el número o el texto exacto de una opción no consume una llamada al LLM.

Los campos de extracción están documentados en `prompt.md` y
`prompts/core_prompt.md`. Los valores no mencionados quedan null; una lista de
categorías vacía explícita elimina el filtro. El backend valida la especificación,
resuelve categorías y calcula todos los importes.

## Datos y renderizado

La consulta es de solo lectura por usuario, fechas, tipo, categorías y moneda, y
excluye movimientos anulados. Nunca convierte ni mezcla monedas. Si existen varias
monedas dentro del conjunto consultado, ofrece opciones antes de renderizar.
Los porcentajes de categoría se calculan sobre el conjunto filtrado, incluyendo Otros.

El PNG usa tipografías mayores, colores consistentes, nombres multilínea y zonas
separadas para título, período, total, barras/leyenda y notas. El pastel presenta
los valores en una leyenda grande sin amontonar porcentajes en sectores pequeños.
Los importes usan formato argentino y omiten decimales si son cero.

Los gráficos temporales se dividen en imágenes de hasta seis barras con una escala
compartida. Con ingresos y gastos se muestran tres meses por imagen; con una serie,
seis meses. El total o saldo del encabezado corresponde al rango completo. Se
identifica cada parte. Streamlit muestra también todas las partes.

`chart_request.py` valida y enruta solicitudes; `chart_query.py` agrega los datos;
`chart_service.py` coordina aclaraciones, preparación y renderizado;
`movement_chart.py` dibuja. El dispatcher solo integra el flujo.
Matplotlib usa FigureCanvasAgg/BytesIO, sin archivos en el servidor: máximo 5 MB por
imagen, dos renders simultáneos y timeout de 8 segundos por imagen.

## Entrega y validación

La entrega reutiliza el cliente de WhatsApp y la idempotencia del webhook.
Un reenvío del mismo webhook no repite la serie. Una falla de envío en una parte
posterior no reenvía las anteriores; el cliente informa el fallo cuando es posible.
Si falla la preparación de cualquiera de las imágenes, no se entrega una serie parcial.

En Streamlit, abrir **Galería de gráficos** para muestras reproducibles de barras,
pastel, nombres largos, valores desiguales, comparaciones y evolución mensual.
La galería funciona sin LLM, sin red y sin consultas a datos personales.
Ofrece vista de 360 px y descarga PNG.

La validación focalizada cubre límites y superposición de etiquetas al renderizar,
aislamiento de datos, monedas, filtros, meses vacíos, cambio de año, base cero,
contexto y opciones, páginas múltiples e idempotencia. La suite completa corre en GitHub.
