from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.authentication import TokenAuthentication
from rest_framework.response import Response
from .cleanup import delete_project_data
from .models import Project
from .serializers import ProjectSerializer

class ProjectListCreateView(generics.ListCreateAPIView):
    serializer_class = ProjectSerializer
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Only return projects owned by the requesting user
        return Project.objects.filter(owner=self.request.user)

class ProjectDetailView(generics.RetrieveUpdateDestroyAPIView):
    """
    Retrieve, rename (PATCH) or delete a single project.

    Deleting also clears the project's files from S3, its document and
    chat records from DynamoDB, and its chunks from Weaviate.
    """
    serializer_class = ProjectSerializer
    authentication_classes = [TokenAuthentication]
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # Only allow retrieving if owned by the requesting user.
        # DRF will automatically return 404 (which serves as a 403-equivalent for existence-hiding)
        # or 403 if object permissions were custom. Filtering the queryset ensures 404 if not owned.
        return Project.objects.filter(owner=self.request.user)

    def destroy(self, request, *args, **kwargs):
        """
        Clear the project's data from every store, then delete the row.

        The Django row goes last, and only on a clean sweep.  It is the
        sole remaining reference to this ``project_id`` in S3, DynamoDB
        and Weaviate, so removing it while data is still out there
        would strand that data permanently.  Keeping the project on a
        failure costs the user a retry; deleting it anyway would cost
        them any way to ever clean up.
        """
        instance = self.get_object()
        project_id = instance.pk

        print(f"\n=== [PROJECT DELETE] Clearing data for project {project_id} ===")
        deleted, errors = delete_project_data(project_id)

        if errors:
            print(f"=== [PROJECT DELETE] Aborted; project {project_id} kept ===\n")
            return Response(
                {
                    "detail": (
                        "The project was not deleted because some of its data "
                        "could not be removed. Nothing has been lost - please "
                        "try again."
                    ),
                    "deleted": deleted,
                    "errors": errors,
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        instance.delete()
        print(f"=== [PROJECT DELETE] Project {project_id} deleted. {deleted} ===\n")
        return Response(status=status.HTTP_204_NO_CONTENT)
