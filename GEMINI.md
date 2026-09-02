# Clarivo — Project Rules

## Backend (Backend-Clarivo/)

- Stack: **Django + Django REST Framework**, SQLite (Django ORM only), boto3 for AWS.
- Always use **DRF generic views or viewsets** (`generics.ListAPIView`,
  `ModelViewSet`, etc.) over plain function views where reasonable.
  Plain `APIView` is acceptable only for one-off endpoints with no standard
  CRUD semantics (e.g., the health-check).
- **Django's database (SQLite) is for users, auth, and project metadata only.**
  Document and file data lives in **DynamoDB** (document records) and **S3**
  (file bytes). Do not store document data in Django models.

## Frontend (Frontend-Clarivo/)

- Stack: **React + Vite + Tailwind CSS**. No other UI or CSS library.
- Use **Tailwind utility classes** for all styling. No inline `style` props,
  no CSS Modules, no styled-components.
- API calls go through the centralised **`src/api/client.js`** axios instance
  (baseURL set from `VITE_API_BASE_URL`). Do not create ad-hoc axios instances
  or use `fetch` directly.

## General

- **Keep changes scoped to what is asked.** Do not refactor, rename, or
  reformat code that is unrelated to the current task.
- This is a student capstone prototype — favour simple, standard, well-documented
  patterns over clever abstractions.
