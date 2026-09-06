/**
 * build.js — Copia los archivos del app web al directorio www/
 * para que Capacitor los empaquete en el APK de Android.
 * Uso: node build.js
 */

const fs   = require('fs');
const path = require('path');

const FILES = [
  'index.html',
  'style.css',
  'app.js',
  'manifest.json',
  'sw.js',
];

const DIRS = [
  ['src/services/openalex.js',  'www/src/services/openalex.js'],
  ['src/services/translate.js', 'www/src/services/translate.js'],
  ['data/stories.json',         'www/data/stories.json'],
];

function ensureDir(filePath) {
  const dir = path.dirname(filePath);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function copyFile(src, dest) {
  ensureDir(dest);
  fs.copyFileSync(src, dest);
  console.log(`  ✓ ${src} → ${dest}`);
}

console.log('\nBuilding PsyHub → www/\n');

// Crear www/
if (!fs.existsSync('www')) fs.mkdirSync('www');

// Copiar archivos raíz
FILES.forEach(f => copyFile(f, path.join('www', f)));

// Copiar archivos de subcarpetas
DIRS.forEach(([src, dest]) => copyFile(src, dest));

console.log('\n✅ Build completo. Ejecutá: npx cap sync\n');
