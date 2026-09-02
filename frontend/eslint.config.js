import js from '@eslint/js';
import globals from 'globals';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';

// This config is the project's static gate. There is no TypeScript and no tsc
// in this repo, so the rules below carry the weight tsc would otherwise:
// prop-types validation is ON (it is our runtime type-checking), and any
// .ts/.tsx file is rejected outright by the no-restricted-imports guard plus
// the CI check in scripts/check_no_typescript.sh.
export default [
  // `.vite/` is Vite's pre-bundled dependency cache (bundled React et al).
  // It lands in the project root when node_modules is a container volume,
  // and linting generated bundles produces hundreds of meaningless errors.
  { ignores: ['dist/**', 'node_modules/**', '.vite/**', 'coverage/**'] },
  js.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: { ...globals.browser, ...globals.es2021 },
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
    },
    settings: { react: { version: 'detect' } },
    plugins: {
      react,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...react.configs.recommended.rules,
      ...react.configs['jsx-runtime'].rules,
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // PropTypes stand in for TypeScript here - keep them mandatory.
      'react/prop-types': 'error',
      'no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
    },
  },
];
