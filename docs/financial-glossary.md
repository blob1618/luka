# Glosario financiero en chat

El glosario de Luka vive en `prompts/financial_glossary.v1.json`. Es contenido
estático, revisado y versionado en Git; no contiene datos de usuarios, historial
ni importes registrados. `FinancialEducationService` lo utiliza para responder
consultas conceptuales sin invocar servicios de persistencia.

## Alcance actual

La versión `1.0.0` cubre presupuesto, gasto fijo, gasto variable, ahorro,
interés simple, interés compuesto, inflación, deuda y costo financiero total
(CFT). Cada entrada tiene definición, ejemplo ilustrativo, sinónimos y fuentes
de revisión.

El servicio pide aclaración para «interés» si no se especifica simple o
compuesto. Para conceptos ausentes, tasas, cotizaciones o valores actuales,
declara la limitación: no completa datos cambiantes ni condiciones de productos.
Las recomendaciones personalizadas de inversión siguen siendo `out_of_scope`.

## Cómo actualizarlo

1. Revisar la definición con una fuente institucional o normativa vigente y
   registrar su nombre en `references`.
2. Agregar o corregir la entrada en el archivo JSON, con un ejemplo puramente
   ilustrativo, breve y coloquial. No incluir tasas ni valores actuales.
3. Si cambia el contenido, incrementar `version`; el valor integra el contenido
   estático que podrá reutilizar el caché de contexto de STK-184.
4. Añadir pruebas para sinónimos, ambigüedades, límites y ausencia de mutaciones.
5. Revisar que la respuesta explique el concepto, no recomiende un producto ni
   prometa resultados financieros.

## Integración con el proveedor

`LLMService` agrega el glosario al prompt estático para reconocer el intent
`financial_education`. El texto por usuario y el historial se incorporan por
separado. Hoy la ruta normal no usa caché remoto; STK-184 podrá cachear solo el
prompt base y este glosario, y deberá volver a la llamada normal si el caché no
es elegible, vence o falla.
