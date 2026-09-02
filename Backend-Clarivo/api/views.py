from rest_framework.views import APIView
from rest_framework.response import Response


class HealthCheckView(APIView):
    """
    GET /api/health/
    Returns {"status": "ok"} to confirm the backend is reachable.
    """

    def get(self, request):
        return Response({"status": "ok"})
