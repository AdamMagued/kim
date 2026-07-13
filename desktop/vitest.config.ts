import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.d.ts',
        'src/**/*.gen.ts',
        'src/**/__tests__/**',
        'src/**/*.test.{ts,tsx}',
        'dist/**',
        'node_modules/**',
      ],
      reporter: ['text', 'json-summary'],
      thresholds: {
        statements: 34,
        branches: 26,
        functions: 27,
        lines: 36,
      },
    },
    server: {
      deps: {
        inline: [/html-encoding-sniffer/, /@exodus\/bytes/],
      },
    },
    deps: {
      optimizer: {
        web: {
          include: ['html-encoding-sniffer', '@exodus/bytes'],
        },
      },
    },
  },
})
