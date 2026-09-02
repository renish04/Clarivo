from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import TokenAuthentication
from .models import Project
from .serializers import ProjectSerializer

class ProjectListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Only return projects owned by the requesting user
        return Project.objects.filter(owner=self.request.user)

class ProjectDetailView(generics.RetrieveAPIView):
    serializer_class = ProjectSerializer
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Only allow retrieving if owned by the requesting user.
        # DRF will automatically return 404 (which serves as a 403-equivalent for existence-hiding) 
        # or 403 if object permissions were custom. Filtering the queryset ensures 404 if not owned.
        return Project.objects.filter(owner=self.request.user)
