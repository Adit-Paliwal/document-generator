import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom";
import "./index.css";
import App from "./App.jsx";
import { ToastProvider } from "./components/ui.jsx";
import Login from "./pages/Login.jsx";
import Dashboard from "./pages/Dashboard.jsx";
import CreateProject from "./pages/CreateProject.jsx";
import ProjectPage from "./pages/ProjectPage.jsx";
import ReviewWorkspace from "./pages/ReviewWorkspace.jsx";
import { getUser } from "./api.js";

function RequireAuth({ children }) {
  return getUser().email ? children : <Navigate to="/login" replace />;
}

const router = createBrowserRouter([
  { path: "/login", element: <Login /> },
  {
    path: "/",
    element: (
      <RequireAuth>
        <App />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Dashboard /> },
      { path: "create", element: <CreateProject /> },
      { path: "project/:projectId", element: <ProjectPage /> },
      { path: "review/:reviewId", element: <ReviewWorkspace /> },
    ],
  },
  { path: "*", element: <Navigate to="/" replace /> },
]);

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <ToastProvider>
      <RouterProvider router={router} />
    </ToastProvider>
  </React.StrictMode>
);
