/**
 * build.js — Prepara los archivos web para Capacitor
 * Copia index.html, app.js, style.css, manifest.json, data/, src/ a ./www/
 */
const fs = require('fs');
const path = require('path');

const SRC = __dirname;
const DEST = path.join(__dirname, 'www');

// Limpiar y crear www/
if (fs.existsSync(DEST)) fs.rmSync(DEST, { recursive: true });
fs.mkdirSync(DEST);

function copyFile(src, dest) {
  const destDir = path.dirname(dest);
  if (!fs.existsSync(destDir)) fs.mkdirSync(destDir, { recursive: true });
  fs.copyFileSync(src, dest);
}

function copyDir(src, dest) {
  if (!fs.existsSync(src)) return;
  if (!fs.existsSync(dest)) fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src)) {
    const srcPath = path.join(src, entry);
    const destPath = path.join(dest, entry);
    if (fs.statSync(srcPath).isDirectory()) {
      copyDir(srcPath, destPath);
    } else {
      copyFile(srcPath, destPath);
    }
  }
}

// Archivos raíz
const rootFiles = ['index.html', 'app.js', 'style.css', 'manifest.json'];
rootFiles.forEach(f => {
  const src = path.join(SRC, f);
  if (fs.existsSync(src)) {
    copyFile(src, path.join(DEST, f));
    console.log('✓ ' + f);
  }
});

// Iconos (si existen)
['icon-192.png','icon-512.png','favicon.ico'].forEach(f => {
  const src = path.join(SRC, f);
  if (fs.existsSync(src)) {
    copyFile(src, path.join(DEST, f));
    console.log('✓ ' + f);
  }
});

// Carpetas
copyDir(path.join(SRC, 'src'), path.join(DEST, 'src'));
console.log('✓ src/');
copyDir(path.join(SRC, 'data'), path.join(DEST, 'data'));
console.log('✓ data/');

console.log('\n✅ Build completado → www/');
