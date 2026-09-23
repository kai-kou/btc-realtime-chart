// Bundles src/main.js (with Lightweight Charts) and copies static files into dist/.
import { build } from 'esbuild';
import { cpSync, mkdirSync, rmSync } from 'node:fs';

rmSync('dist', { recursive: true, force: true });
mkdirSync('dist');
cpSync('public', 'dist', { recursive: true });
await build({
  entryPoints: ['src/main.js'],
  bundle: true,
  format: 'esm',
  minify: true,
  sourcemap: true,
  target: 'es2022',
  outfile: 'dist/app.js',
  logLevel: 'info',
});
