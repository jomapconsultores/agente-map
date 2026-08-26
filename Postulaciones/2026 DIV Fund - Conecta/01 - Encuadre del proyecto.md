# Conecta — encuadre para DIV Fund, Etapa 1

**Postulante:** CMAJ Asociados S.A.S. · RUC 0195146942001 · Cuenca, Ecuador
**Etapa:** 1 — piloto · **Techo:** USD 200.000 · **Duración sugerida:** 18 meses
**Base técnica del producto:** `06_DESARROLLO\Vinculación UCuenca\vinculacion`

---

## 1. El encuadre, y por qué importa tanto

Conecta se describe hoy, en su propio README, como una «plataforma institucional de vinculación con
graduados» con una bolsa de empleo. Presentado así, **DIV lo rechaza sin leer el resto**: su lista de
exclusiones nombra explícitamente a los intermediarios, y una bolsa de empleo con emparejamiento es
un intermediario de manual.

El encuadre que sí resiste el filtro no es la plataforma. Es el mecanismo:

> **Conecta convierte una brecha de competencias detectada en una credencial verificable y avalada
> por una universidad, apoyándose en el registro nacional de títulos del Estado.**

Lo que se financia no es el software de emparejamiento; es el **circuito cerrado** que hoy no existe
en ningún país de la región:

```
título verificado contra el registro nacional
        ↓
brecha detectada frente a una vacante real
        ↓
curso concreto que cierra esa brecha específica
        ↓
aval institucional de la competencia adquirida
        ↓
credencial verificable que el empleador puede comprobar
```

Cada flecha existe por separado en el mercado. **Ninguna solución las tiene encadenadas**, y la
razón es que ese encadenamiento requiere estar conectado al registro oficial de títulos, que es
justo lo que Conecta ya resuelve.

## 2. El problema, con las cifras del INEC

El desempleo en Ecuador es del **3,1 %** (mayo de 2026) y eso hace que el problema pase inadvertido.
El problema real está en la calidad del empleo:

| Indicador (INEC, mayo 2026) | |
|---|---|
| Empleo adecuado | **36,6 %** |
| Subempleo | 18,3 % |
| Otro empleo no pleno | 32,1 % |
| Desempleo | 3,1 % |

**Casi dos de cada tres personas ocupadas en Ecuador no tienen empleo adecuado.** El país no tiene un
problema de falta de trabajo: tiene un problema de emparejamiento entre lo que la gente sabe hacer y
lo que el mercado necesita, agravado porque el empleador no tiene forma barata de verificar
competencias y el graduado no tiene forma de saber qué le falta.

Ese doble desconocimiento es el que produce el resultado observable: gente con título trabajando en
lo que no estudió, y vacantes que se declaran difíciles de llenar en el mismo territorio.

## 3. Teoría del cambio

| | |
|---|---|
| **Supuesto central** | El graduado no consigue empleo adecuado no por falta de vacantes, sino porque su competencia ni es visible para el empleador ni es diagnosticable para sí mismo. |
| **Intervención** | Verificar el título contra el registro nacional, diagnosticar la brecha frente a vacantes reales, dirigir a un curso específico y avalar institucionalmente la competencia adquirida. |
| **Producto** | Credencial verificable, emitida por la universidad, atada a una competencia concreta y no a un curso genérico. |
| **Resultado inmediato** | El empleador puede comprobar la competencia sin entrevistar; el graduado sabe exactamente qué le falta y cuánto cuesta cerrarlo. |
| **Resultado intermedio** | Menor tiempo hasta la colocación y mayor proporción de colocaciones en empleo adecuado. |
| **Impacto** | Reducción de la brecha entre formación y empleo adecuado, medida sobre la cohorte piloto frente a un grupo de comparación. |

## 4. Qué está construido y qué falta

Esto es lo que hace que la postulación sea a Etapa 1 y no a una idea sobre papel. **Existe y
funciona:**

- Aplicación Next.js 14 con Supabase, autenticación verificada por correo y RLS, desplegada.
- **Integración con el registro nacional de títulos**: consulta por cédula, con limitación de tasa y
  entrega diferenciada según haya o no sesión.
- Autollenado del perfil desde el padrón: nombre, cédula, carrera, título.
- Generación y análisis de CV con IA, con cadena de respaldo entre tres proveedores.
- Análisis de experiencia, publicaciones y cursos.
- **Ranking de candidatos por competencias** para el empleador, con caché de 24 horas por criterio
  explícito de costo: no se reevalúa a un candidato cuyo perfil no ha cambiado.
- Módulo de empleador: empresa, vacantes, candidatos, ranking.
- Emisión y verificación pública de certificados por código.
- Módulos de prácticas preprofesionales, servicios comunitarios, psicometría, encuestas e
  indicadores con exportación a Excel, PDF y Word.
- 60 rutas de API y 18 migraciones de base de datos versionadas.

**No existe todavía, y es exactamente lo que el piloto debe producir:**

- Datos reales. El padrón actual es sembrado, con seis cédulas de prueba.
- Convenio con una universidad que **avale** competencias, no solo que emita certificados.
- Empleadores reales usando el ranking en decisiones de contratación reales.
- **Evidencia de impacto.** Hoy no hay ninguna. Esa es la razón de ser de la Etapa 1.

## 5. Diseño del piloto

**Sitio:** una universidad en Cuenca como primer nodo, con una segunda institución en otra provincia
para probar la replicabilidad desde el inicio. Este segundo nodo no es opcional: sin él, la propuesta
cae en la exclusión de «contextos muy acotados».

**Cohorte:** graduados recientes en situación de subempleo o empleo no pleno, que es la población que
el INEC identifica como mayoritaria y a la que el fondo exige que se beneficie directamente.

**Grupo de comparación:** graduados de la misma cohorte y carrera que acceden al perfil verificado
pero no al circuito de aval de competencias. Sin este grupo no hay evidencia atribuible, y sin
evidencia atribuible no hay Etapa 2.

**Métricas primarias**

1. Proporción de la cohorte que alcanza empleo adecuado a los 6 y 12 meses, frente al grupo de
   comparación.
2. Tiempo mediano hasta la colocación.

**Métricas secundarias**

3. Brechas diagnosticadas que efectivamente se cierran con un curso completado y avalado.
4. Tasa de uso del ranking por parte de empleadores en decisiones reales de contratación.
5. Costo por colocación en empleo adecuado.

## 6. Costo-efectividad

El argumento es defendible y hay que sostenerlo con números del piloto, no con adjetivos.

El costo marginal de diagnosticar la brecha de un graduado adicional es el de unas pocas llamadas a
un modelo de lenguaje —céntimos— más el almacenamiento. La arquitectura ya está optimizada en esa
dirección: el caché de 24 horas del ranking existe precisamente porque recalcular a cada clic
multiplicaba el costo sin cambiar el resultado.

La comparación relevante no es contra otra plataforma, sino contra el mecanismo que hoy cumple esa
función: **la intermediación laboral presencial y las ferias de empleo**, que tienen costo por
persona atendida de dos órdenes de magnitud superior y no dejan credencial verificable.

Ese contraste —costo marginal cercano a cero frente a intermediación presencial— es el que hay que
cuantificar durante el piloto.

## 7. Ruta hacia la escala

DIV pide declarar si la ruta es pública, comercial o híbrida. Aquí es **híbrida**, y conviene decirlo
con precisión:

- **Vía pública.** La verificación se apoya en el registro nacional de títulos, que cubre a todo el
  país. Cualquier universidad ecuatoriana es un nodo incorporable sin rehacer la infraestructura: es
  el mismo padrón. La adopción por la autoridad de educación superior o por el ministerio de trabajo
  convertiría el circuito en política pública sin costo marginal de plataforma.
- **Vía comercial.** El empleador paga por publicar vacantes y usar el ranking; el graduado nunca
  paga. Esto sostiene la operación sin trasladar costo a la población beneficiaria.

**El potencial de un millón de personas** —umbral explícito del fondo— se sostiene sobre el stock de
titulados registrados en el sistema nacional, no sobre la matrícula de una universidad. Es el número
que hay que citar con fuente en la propuesta y todavía no lo tenemos: ver el documento de datos
pendientes.

## 8. Los tres riesgos que hay que responder antes de que los pregunten

**Que se lea como intermediario.** Mitigación: el producto financiado es el mecanismo de aval de
competencias y su credencial verificable. La bolsa de empleo es el banco de pruebas donde se mide si
la credencial sirve, no el objeto del financiamiento. Toda la propuesta debe estar escrita en ese
orden.

**Que se lea como proyecto de una sola universidad.** Mitigación: dos nodos en provincias distintas
desde el primer año, y el argumento de que la columna vertebral es un registro nacional.

**Dependencia de la integración con el registro de títulos.** Es la mayor fortaleza técnica y también
el mayor riesgo: si cambia el acceso, se cae la verificación. Mitigación: formalizar el acceso por
convenio durante el piloto, en lugar de depender de una consulta pública, y documentarlo como
resultado del proyecto.

## 9. Lo que hay que resolver antes de enviar

1. **Convenio con la universidad piloto**, con compromiso explícito de avalar competencias. Sin una
   carta firmada, la teoría del cambio se rompe en su eslabón central.
2. **Segundo nodo** en otra provincia.
3. **Dato de escala** con fuente citable.
4. **Presupuesto** a 18 meses.
5. **Equipo**: quién dirige la evaluación de impacto. DIV valora la evaluación rigurosa como
   componente, no como adorno; conviene un socio académico que la conduzca.
6. **Revisar la exclusión de «alto ingreso»**: el proyecto beneficia a graduados universitarios, y
   hay que argumentar por qué esa población no es «beneficiarios de ingreso alto» en el contexto
   ecuatoriano. Los datos del INEC sobre empleo adecuado son la respuesta, y hay que ponerla por
   delante.
