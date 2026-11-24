const typescriptParser = require('@typescript-eslint/parser');
const typescriptPlugin = require('@typescript-eslint/eslint-plugin');
const reactHooks = require('eslint-plugin-react-hooks');

const hooks = reactHooks.configs.flat['recommended-latest'];

module.exports = [
    {
        ignores: ['node_modules/', 'dist/']
    },
    {
        files: ['**/*.ts', '**/*.tsx'],
        languageOptions: {
            parser: typescriptParser,
            parserOptions: {
                ecmaVersion: 'latest',
                sourceType: 'module'
            }
        },
        plugins: {
            '@typescript-eslint': typescriptPlugin,
            ...hooks.plugins
        },
        rules: {
            // Rules of hooks, exhaustive deps and the React Compiler's checks; a missing dependency fails the gate.
            ...hooks.rules,
            'react-hooks/exhaustive-deps': 'error',

            semi: ['error', 'always'],
            quotes: ['error', 'single', {avoidEscape: true}],
            'eol-last': ['error', 'always'],
            'prefer-const': 'error',
            'no-var': 'error',
            eqeqeq: ['error', 'always'],
            'no-unused-vars': 'off',
            'no-console': ['error', {allow: ['warn', 'error']}],
            'no-eval': 'error',
            'no-implied-eval': 'error',
            // The icons' barrel is all of MUI's ten thousand icons: the dev server sends and runs it whole on each load.
            'no-restricted-imports': [
                'error',
                {
                    paths: [
                        {
                            name: '@mui/icons-material',
                            message: 'Import each icon by its path: @mui/icons-material/<Name>.'
                        }
                    ]
                }
            ],

            '@typescript-eslint/consistent-type-definitions': ['error', 'interface'],
            '@typescript-eslint/no-unused-vars': [
                'error',
                {args: 'none', ignoreRestSiblings: true, varsIgnorePattern: '^_', caughtErrors: 'none'}
            ],
            '@typescript-eslint/no-inferrable-types': 'error',
            '@typescript-eslint/no-non-null-assertion': 'error',
            '@typescript-eslint/no-explicit-any': 'error'
        }
    }
];
