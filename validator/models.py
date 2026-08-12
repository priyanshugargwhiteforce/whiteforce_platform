from django.db import models


class ValidationHistory(models.Model):
    email = models.EmailField()
    score = models.IntegerField(default=0)

    provider = models.CharField(
        max_length=50,
        default='other'
    )

    status = models.CharField(
        max_length=20,
        default='invalid'
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    def __str__(self):
        return self.email