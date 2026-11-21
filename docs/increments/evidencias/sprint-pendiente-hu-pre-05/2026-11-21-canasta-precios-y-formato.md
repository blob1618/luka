# Canasta: presentación, precios y ofertas

- Fecha: 2026-11-21. Rama: `codex/stk-263-basket-detail-feedback`.
- Evidencia comunicada por la persona usuaria desde WhatsApp real: la propuesta y la canasta se mostraron en una sola línea; «¿Querés consultar precios?» ofreció el botón «Consultar ofertas», que respondió sin promociones aunque SEPA sí tenía precios.
- Causa: la canasta sólo guardaba nombre, cantidad y código interno. La consulta de precios y la de promociones compartían la misma acción de botón; la propuesta y la lectura unían productos con punto y coma.
- Corrección: propuesta y lectura en líneas numeradas. Al leer la canasta se consulta el menor precio publicado del snapshot SEPA vigente y se indica comercio, sucursal y fecha. Es una referencia de precio, no una compra ni un precio histórico guardado. El botón de la canasta compara precios por sucursal; la alerta de presupuesto y la consulta explícita de ofertas buscan sólo promociones verificadas. Si no hay promociones, la respuesta diferencia esa ausencia de la disponibilidad de precios.
- Verificación local: 9 pruebas focalizadas de canasta, [204 unitarias](20261121T213714Z-unit-8f905b59.json), [lint](20261121T213714Z-lint-fee141b6.json) y [suite completa de 1372 pruebas](20261121T213733Z-suite-85095925.json) aprobados. La prueba de esta versión por WhatsApp real y la conclusión humana siguen pendientes.
