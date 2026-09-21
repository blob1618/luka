# Gráficos financieros en WhatsApp (STK-53)

## Alcance

Luka genera un PNG cuando el usuario pide explícitamente un gráfico o diagrama de
gastos, egresos o ingresos por categoría. El gráfico predeterminado es de barras y el
período predeterminado es el mes actual en `America/Argentina/Buenos_Aires`.

La consulta es de solo lectura. Filtra por usuario, tipo, período, moneda y movimientos
no anulados. Nunca convierte moneda ni combina monedas diferentes.

## Selección de categorías

- `mayor`, `mayores` o un pedido sin ranking: cinco categorías con mayor importe.
- `menor`, `menores` o `menos gasto`: cinco categorías con menor importe.
- Cuando hay más de cinco categorías, las restantes se agrupan como `Otros`.
- El total de los seis elementos coincide con el total consultado.

## Entrega y fallos

Matplotlib renderiza con `FigureCanvasAgg` sobre `BytesIO`. No se crean archivos en el
servidor. La imagen tiene dimensiones fijas, un máximo de seis elementos y un límite de
5 MB. El render se ejecuta fuera del event loop, con concurrencia acotada y timeout.

El cliente de WhatsApp carga el PNG al endpoint de medios y envía el `media_id` obtenido.
Si la entrega falla, intenta informar el problema mediante texto. Ante un timeout ambiguo
del POST de la imagen no vuelve a enviar el medio: se prioriza evitar un duplicado y se
completa la clave idempotente después del fallback.

## Validación manual

Probar con un usuario que tenga movimientos de varias categorías:

1. `Mostrame un gráfico de barras de mis gastos de este mes`.
2. `Haceme una torta de las categorías con mayor gasto en septiembre`.
3. `Mostrame un gráfico de las categorías con menor egreso`.
4. Repetir un pedido sobre un período con ARS y USD y seleccionar una moneda.
5. Pedir un período sin movimientos y comprobar que solo llega texto.
6. Reenviar el mismo webhook y comprobar que no llega una segunda imagen.

Verificar que el PNG muestre título, período, moneda, etiquetas e importe total, y que
los nombres largos sigan siendo legibles en la vista móvil de WhatsApp.
