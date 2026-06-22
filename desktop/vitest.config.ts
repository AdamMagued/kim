import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
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
