# Ficha de incremento — [HU-XXX-NN / trabajo técnico] · [título]

> Copiá esta ficha en `docs/increments/` como `sprint-NN-hu-xxx-nn.md` al iniciar el trabajo. Para trabajo exclusivamente técnico usá `sprint-NN-<tema>.md`. Agrupá en la misma ficha los PR y correcciones de una HU. Si un PR cubre varias HU, referencialo desde la ficha de cada una.
>
> El autor actualiza la ficha y la [descripción de cada PR](../../.github/pull_request_template.md) conforme aparezcan evidencias, incluso después del merge. Enlazá el registro original en GitHub, Jira o el acta del equipo. Usá fechas `AAAA-MM-DD`, «Pendiente» si falta evidencia y «No aplica» con motivo cuando corresponda. Repetí las filas según sea necesario y usá datos ficticios o anonimizados en logs, capturas y ejemplos.

## 1. Alcance y compromiso

| Campo | Completar |
| --- | --- |
| Historia y valor | [Indicador HU, título, descripción breve y beneficio para el usuario. Para trabajo técnico, propósito y HU a la que apoya si existe] |
| Responsables y referencias | [Autor, persona designada para validar, enlaces a Jira, PR del backend o frontend y dependencias] |
| Release | [Número, Release Goal, estado y decisión de alcance con enlace a la planificación o acta] |
| Priorización y estimación | [MoSCoW, alcance priorizado, SP acordados por Planning Poker y fuente. Trabajo técnico sin SP de HU] |
| Sprint | [Número, período, Sprint Goal y aporte concreto de la HU] |
| Selección y capacidad | [Fecha y asistentes de Sprint Planning, capacidad acordada, compromiso de la HU, total comprometido del sprint y enlace al acta] |
| Preparación | [Origen de los criterios de aceptación, dependencias, riesgos, mitigaciones y trabajo técnico de apoyo] |
| Estado de la HU | [Planificada / En curso / En validación / Hecho / Replanificado, fecha y fuente] |

Usá el indicador HU al trasladar la información a la planificación. Jira y los PR sirven como fuentes de evidencia. Los SP acordados se conservan durante el sprint y al replanificar. Las subtareas y los PR adicionales de una HU se registran como apoyo sin sumar puntos.

## 2. Implementación y operación

- **Resultado entregado** [Qué puede hacer ahora el usuario o qué capacidad técnica quedó disponible. Comparar con el alcance comprometido e identificar diferencias y motivos].
- **Diseño y cambios** [Recorrido anterior y nuevo cuando corresponda, componentes, contratos, decisiones y justificación. Enlaces a código, PR, documentación o ADR].
- **Datos e integraciones** [Cambios de modelo, migraciones versionadas, compatibilidad de datos existentes, servicios externos, permisos y configuración por ambiente. Si hay migraciones, registrar historial y comprobación de objetos o datos en el entorno aplicado].
- **Operación y recuperación** [Secuencia de despliegue, configuración requerida por nombre, comprobaciones posteriores, señales de fallo y pasos de mitigación o reversión, cuando apliquen].
- **Riesgos y límites** [Casos fuera de alcance, errores conocidos, privacidad y degradación esperada. Enlazar los seguimientos].

| Hito | Fecha | Responsable | Evidencia y versión |
| --- | --- | --- | --- |
| Inicio efectivo del trabajo | [Fecha] | [Persona] | [Registro o Jira] |
| Implementación lista para revisión | [Fecha] | [Persona] | [PR y SHA revisable] |
| Revisión de código y merge | [Fechas respectivas] | [Personas] | [Revisión, PR y SHA integrado] |
| Despliegue y verificación en Render | [Fecha] | [Quien integra] | [Ambiente, SHA desplegado, registro y resultado del smoke test] |
| Validación humana terminada | [Fecha] | [Persona designada] | [Conclusión explícita en PR o Jira] |
| Cierre en Jira | [Fecha] | [Persona] | [Transición o registro] |

Registrá cada hito con su propia evidencia. Si hubo correcciones posteriores, agregá sus PR, versiones y fechas. Si las versiones revisada, probada, integrada y desplegada difieren, indicá qué evidencia corresponde a cada una.

## 3. Verificación técnica y aceptación

| Prueba | Fecha, ambiente y versión | Preparación y pasos reproducibles | Resultado esperado y observado | Evidencia |
| --- | --- | --- | --- | --- |
| [Unitarias, integración, regresión, lint, CI o smoke test según el cambio] | [Fecha, ambiente, SHA y responsable] | [Comando o pasos, configuración y datos de prueba] | [Resultado, fallos y alcance] | [Ejecución, log o artefacto] |

| Criterio de aceptación de la HU | Caso, precondiciones y entrada | Resultado esperado y observado | Medio, versión y evidencia | Conclusión humana |
| --- | --- | --- | --- | --- |
| [Criterio acordado y fuente] | [Pasos, mensajes o datos de prueba y casos límite relevantes] | [Respuesta, estado persistido o efecto observable] | [WhatsApp real / simulador / otro, ambiente, SHA y enlace a evidencia] | [Pendiente / Aceptado / Rechazado, persona, fecha y enlace al registro] |

- **Demostración y conclusión** [Guion breve o enlace a la demo, criterios aceptados, pendientes y rechazados, y conclusión explícita de la persona designada].
- **Definition of Done** [Evidencia de código integrado, pruebas aprobadas, documentación alineada y demostración aceptada. Indicar qué falta para completar la HU].

Usá una fila por criterio y agregá los casos necesarios. Registrá el canal efectivamente utilizado. CI, el simulador, el despliegue y el estado de Jira tienen evidencias propias. La aceptación requiere la conclusión explícita de la persona designada en el PR o Jira, con fecha y enlace.

## 4. Seguimiento durante el sprint

| Fecha y foco | Avance observable y siguiente acción | Impedimento o cambio | Acción correctiva y responsable | Impacto en el Sprint Goal y evidencia |
| --- | --- | --- | --- | --- |
| [Día activo y HU o trabajo técnico] | [Resultado y próximo paso] | [Bloqueo, cambio de alcance o Ninguno] | [Acción, responsable y fecha] | [Efecto y enlace a Daily, PR, Jira o prueba] |

| Cierre del día activo | Puntos pendientes del sprint | Cambio respecto del día anterior y motivo | Fuente |
| --- | --- | --- | --- |
| [Fecha] | [SP observados] | [HU aceptada o replanificada, ajuste de alcance o sin variación] | [Burndown compartido, Jira o acta] |

Al cierre de cada día activo, actualizá el burndown compartido o enlazá su registro desde la ficha. Para la velocidad, computá los SP de las HU aceptadas en la Sprint Review. Si se mueve una HU, documentá el traslado por separado del trabajo aceptado.

## 5. Sprint Review, cierre y replanificación

| Campo | Completar |
| --- | --- |
| Review | [Fecha, asistentes, acta y evidencia de lo demostrado] |
| Resultado respecto del Sprint Goal | [Cumplido / parcial / no cumplido, contribución del incremento y evidencia] |
| Compromiso y resultado aceptado | [Indicadores HU, SP comprometidos y aceptados, criterios acreditados, enlace a la Review y al burndown observado] |
| Trabajo sin completar | [Indicadores HU, criterios y SP no aceptados, bloqueos, defectos o validaciones pendientes] |
| Cierre o replanificación | [Decisión explícita, fecha, responsable y evidencia. Si se traslada, indicar HU, SP, sprint de origen, sprint de destino o backlog, motivo y enlace] |
| Remanente y feedback | [Pedido de stakeholders, decisión del PO sobre el Product Backlog, responsable, ticket o enlace y próximo paso] |

Para un traslado, registrá «Replanificado» en origen y una sola aparición con el estado actualizado en destino. Conservá el indicador y los SP, y contá la HU una sola vez en el resumen consolidado de releases.

## 6. Sprint Retrospective

- **Sesión** [Fecha, asistentes y enlace al acta].
- **Acciones del sprint anterior** [Qué se verificó, resultado y evidencia].
- **Qué funcionó y qué falló** [Resultados concretos, impacto y fuentes].
- **Causa** [Causa validada por el equipo o hipótesis pendiente de comprobar].

| Acción de mejora (hasta tres) | Responsable | Fecha límite o sprint | Cómo se verificará y dónde |
| --- | --- | --- | --- |
| [Cambio verificable] | [Persona o rol] | [Fecha o sprint] | [Indicador, resultado esperado y enlace de seguimiento] |

## 7. Traslado a la planificación

| Sección de «03 — Planificación» | Fuente en la ficha | Fecha y enlace a la actualización |
| --- | --- | --- |
| Release Goal, estado y alcance. MoSCoW y Planning Poker | Alcance y compromiso | [Fecha y enlace, o Pendiente] |
| Tabla del sprint. Historia, descripción, criterios, SP, estado y fecha de cierre | Alcance, hitos y aceptación | [Fecha y enlace, o Pendiente] |
| Sprint Planning, Daily Scrum y burndown observado | Compromiso y seguimiento | [Fecha y enlace, o Pendiente] |
| Sprint Review y decisión de cierre o replanificación | Aceptación, resultado y remanente | [Fecha y enlace, o Pendiente] |
| Sprint Retrospective y acciones de mejora | Retrospectiva y seguimiento | [Fecha y enlace, o Pendiente] |
