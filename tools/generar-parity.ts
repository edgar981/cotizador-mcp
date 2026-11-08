/**
 * Genera tests/fixtures/parity.json ejecutando las funciones TypeScript del
 * cotizador. NO se copian los literales de los tests a mano: se ejecuta el
 * código real y se vuelca lo que devuelve.
 *
 * Se corre con `npx tsx` desde el repo del cotizador, que solo se LEE.
 */
import { writeFileSync } from "node:fs";
import { execSync } from "node:child_process";
import { calcularCotizacion, type ParametrosCotizacion } from "../../cotizador-personal-shopper/lib/cotizador";
import { calcularEnvioNacional, buscarZona } from "../../cotizador-personal-shopper/lib/envio";
import { calcularComision, porcentajeVisible } from "../../cotizador-personal-shopper/lib/comision";
import { normalizarZonas, ZONAS_ENVIO_DEFAULT, type ZonaEnvio } from "../../cotizador-personal-shopper/lib/settings-defaults";

const base: ParametrosCotizacion = {
  precio_usd: 100, peso_lb: 2.6, sales_tax_pct: 0.07, tarifa_lb_usd: 6,
  trm_oficial: 4000, trm_buffer_pct: 0.02, margen_pct: 0.25, redondeo_cop: 5000,
};

// --- cotizar -------------------------------------------------------------
const casosCotizar: Array<{ nombre: string; entrada: ParametrosCotizacion }> = [
  { nombre: "tenis, caso base del test", entrada: base },
  { nombre: "subtotal exacto sobre el múltiplo", entrada: { ...base, peso_lb: 0, sales_tax_pct: 0, tarifa_lb_usd: 0, trm_buffer_pct: 0 } },
  { nombre: "un centavo por encima del múltiplo", entrada: { ...base, precio_usd: 100.01, peso_lb: 0, sales_tax_pct: 0, tarifa_lb_usd: 0, trm_buffer_pct: 0 } },
  { nombre: "perfume con TRM real y redondeo a 1000", entrada: { precio_usd: 89.99, peso_lb: 1.1, sales_tax_pct: 0.07, tarifa_lb_usd: 6, trm_oficial: 3204.51, trm_buffer_pct: 0.02, margen_pct: 0.3, redondeo_cop: 1000 } },
  { nombre: "sin redondeo (redondeo_cop = 0)", entrada: { ...base, redondeo_cop: 0 } },
  { nombre: "redondeo negativo cae en el mismo paso 1", entrada: { ...base, redondeo_cop: -5000 } },
  { nombre: "precio y peso NaN se tratan como cero", entrada: { ...base, precio_usd: NaN, peso_lb: NaN } },
  { nombre: "precio y peso negativos se recortan a cero", entrada: { ...base, precio_usd: -50, peso_lb: -3 } },
  // Empates de redondeo: Math.round sube, round() de Python usa banquero.
  { nombre: "empate a .5 en el tax (0.125 USD)", entrada: { ...base, precio_usd: 2.5, sales_tax_pct: 0.05, peso_lb: 0, tarifa_lb_usd: 0, trm_buffer_pct: 0 } },
  { nombre: "empate a .5 en el flete (0.125 USD)", entrada: { ...base, precio_usd: 0, sales_tax_pct: 0, peso_lb: 0.25, tarifa_lb_usd: 0.5, trm_buffer_pct: 0 } },
  { nombre: "empate a .005 en el margen", entrada: { ...base, precio_usd: 1, peso_lb: 0, sales_tax_pct: 0, tarifa_lb_usd: 0, trm_oficial: 1, trm_buffer_pct: 0, margen_pct: 0.045, redondeo_cop: 1 } },
  { nombre: "empate a .5 en la TRM aplicada", entrada: { ...base, trm_oficial: 1000.25, trm_buffer_pct: 0 } },
  { nombre: "todo en cero", entrada: { precio_usd: 0, peso_lb: 0, sales_tax_pct: 0, tarifa_lb_usd: 0, trm_oficial: 0, trm_buffer_pct: 0, margen_pct: 0, redondeo_cop: 5000 } },
  { nombre: "valores grandes (ruido de punto flotante)", entrada: { ...base, precio_usd: 1999.99, peso_lb: 12.7, trm_oficial: 4987.63 } },
];

// --- envío ---------------------------------------------------------------
const bogota: ZonaEnvio = { nombre: "Bogotá", tarifa_base_cop: 12000, adicional_lb_cop: 2000 };
const plana: ZonaEnvio = { nombre: "Bogotá", tarifa_base_cop: 16000, adicional_lb_cop: 0 };
const casosEnvio: Array<{ nombre: string; zona: ZonaEnvio; peso_lb: number }> = [
  { nombre: "redondea a 500", zona: bogota, peso_lb: 2.6 },
  { nombre: "bruto exacto sobre el múltiplo", zona: bogota, peso_lb: 1.25 },
  { nombre: "tarifa base ya múltiplo de 500", zona: { ...bogota, adicional_lb_cop: 0 }, peso_lb: 3.4 },
  { nombre: "peso cero: solo tarifa base", zona: { ...bogota, tarifa_base_cop: 15000 }, peso_lb: 0 },
  { nombre: "tarifa plana, peso liviano", zona: plana, peso_lb: 0.5 },
  { nombre: "tarifa plana, peso medio", zona: plana, peso_lb: 2.6 },
  { nombre: "tarifa plana, peso alto", zona: plana, peso_lb: 12 },
  { nombre: "peso negativo", zona: bogota, peso_lb: -5 },
  { nombre: "tarifa base negativa", zona: { ...bogota, tarifa_base_cop: -1 }, peso_lb: 0 },
  { nombre: "adicional negativo", zona: { ...bogota, adicional_lb_cop: -100 }, peso_lb: 2 },
  { nombre: "zona gratis no devuelve -0", zona: { nombre: "X", tarifa_base_cop: 0, adicional_lb_cop: 0 }, peso_lb: 0 },
  { nombre: "un peso por encima del múltiplo", zona: { nombre: "X", tarifa_base_cop: 16001, adicional_lb_cop: 0 }, peso_lb: 0 },
  { nombre: "peso fraccionario largo", zona: bogota, peso_lb: 1.0 / 3.0 },
];

const zonas = ZONAS_ENVIO_DEFAULT;
const casosBuscarZona = ["Bogotá", "  bogotá  ", "BOGOTÁ", "Bogota", "Municipio", "no existe", "", "   "];

const casosNormalizarZonas: Array<{ nombre: string; entrada: unknown }> = [
  { nombre: "null = anterior a la migración", entrada: null },
  { nombre: "undefined = lo mismo", entrada: undefined },
  { nombre: "array vacío = apagado a propósito", entrada: [] },
  { nombre: "no es un array (string)", entrada: "texto" },
  { nombre: "no es un array (objeto)", entrada: { nombre: "Bogotá" } },
  { nombre: "descarta filas incompletas, negativas y repetidas", entrada: [
    { nombre: "Bogotá", tarifa_base_cop: 12000, adicional_lb_cop: 2000 },
    { nombre: "  ", tarifa_base_cop: 1, adicional_lb_cop: 1 },
    { nombre: "Cali", tarifa_base_cop: -1, adicional_lb_cop: 1 },
    { nombre: "Cali", tarifa_base_cop: 1, adicional_lb_cop: -1 },
    { nombre: "bogota", tarifa_base_cop: 9, adicional_lb_cop: 9 },
    null, "x", 5,
  ] },
  { nombre: "tarifas en cero son válidas", entrada: [{ nombre: "Recoge", tarifa_base_cop: 0, adicional_lb_cop: 0 }] },
  { nombre: "recorta espacios del nombre", entrada: [{ nombre: "  Cartagena  ", tarifa_base_cop: 18500, adicional_lb_cop: 0 }] },
  { nombre: "acepta números como texto", entrada: [{ nombre: "Cali", tarifa_base_cop: "18500", adicional_lb_cop: "0" }] },
];

// --- comisión ------------------------------------------------------------
const casosComision: Array<[string, number, number]> = [
  ["reparto normal", 125052, 0.1],
  ["0% no reparte", 125052, 0],
  ["100% no deja neto", 80000, 1],
  ["margen cero", 0, 0.2],
  ["margen negativo", -5000, 0.1],
  ["pct negativo", 10000, -0.1],
  ["ruido de punto flotante", 0.3, 0.1],
  ["margen típico al 12%", 50000, 0.12],
  ["empate a .005", 0.05, 0.1],
  ["empate a .5 centavos", 12.345, 0.5],
  ["pct mayor que 1", 10000, 1.5],
  ["margen grande", 1234567.89, 0.075],
];

const commit = execSync("git -C ../cotizador-personal-shopper rev-parse HEAD").toString().trim();

const fixture = {
  _fuente: {
    repo: "cotizador-personal-shopper",
    commit,
    modulos: ["lib/cotizador.ts", "lib/envio.ts", "lib/comision.ts", "lib/settings-defaults.ts"],
    generado_por: "tools/generar-parity.ts",
    nota: "Salidas producidas EJECUTANDO el TypeScript, no copiadas de los tests.",
  },
  calcular_cotizacion: casosCotizar.map((c) => ({
    nombre: c.nombre,
    // NaN no existe en JSON: se marca para que el port lo reconstruya.
    entrada: Object.fromEntries(Object.entries(c.entrada).map(([k, v]) => [k, Number.isNaN(v) ? "NaN" : v])),
    esperado: calcularCotizacion(c.entrada),
  })),
  calcular_envio_nacional: casosEnvio.map((c) => ({
    nombre: c.nombre, zona: c.zona, peso_lb: c.peso_lb,
    esperado: calcularEnvioNacional(c.zona, c.peso_lb),
  })),
  buscar_zona: casosBuscarZona.map((nombre) => ({
    nombre_buscado: nombre, zonas, esperado: buscarZona(zonas, nombre),
  })),
  normalizar_zonas: casosNormalizarZonas.map((c) => ({
    nombre: c.nombre,
    entrada: c.entrada === undefined ? "__undefined__" : c.entrada,
    esperado: normalizarZonas(c.entrada),
  })),
  calcular_comision: casosComision.map(([nombre, margen, pct]) => ({
    nombre, margen_cop: margen, comision_pct: pct,
    esperado: calcularComision(margen, pct),
  })),
  porcentaje_visible: [0, 0.1, 0.075, 1, 1.5, 0.12345, -0.1].map((f) => ({
    fraccion: f, esperado: porcentajeVisible(f),
  })),
};

writeFileSync("../cotizador-mcp/tests/fixtures/parity.json", JSON.stringify(fixture, null, 2) + "\n");
const total = fixture.calcular_cotizacion.length + fixture.calcular_envio_nacional.length +
  fixture.buscar_zona.length + fixture.normalizar_zonas.length +
  fixture.calcular_comision.length + fixture.porcentaje_visible.length;
console.log(`parity.json: ${total} vectores desde el commit ${commit.slice(0, 7)}`);
