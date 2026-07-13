/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Adani purple palette
        brand: {
          50:  "#f5f0fa",
          100: "#e9dcf5",
          200: "#d3b9eb",
          300: "#b08adf",
          400: "#8e5cc9",
          500: "#75379f",   // primary
          600: "#632d87",
          700: "#52246f",
          800: "#411b58",
          900: "#301340",
        },
      },
      fontFamily: {
        sans: ["Segoe UI", "system-ui", "-apple-system", "sans-serif"],
      },
    },
  },
  plugins: [],
};
