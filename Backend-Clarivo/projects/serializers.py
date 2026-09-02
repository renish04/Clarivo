from rest_framework import serializers
from .models import Project

class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ['id', 'name', 'owner', 'created_at']
        read_only_fields = ['owner', 'created_at']

    def create(self, validated_data):
        # Automatically assign the requesting user as the owner
        validated_data['owner'] = self.context['request'].user
        return super().create(validated_data)

