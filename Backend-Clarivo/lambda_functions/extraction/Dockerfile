FROM public.ecr.aws/lambda/python:3.12

# Install Python dependencies first (layer is cached unless requirements change).
COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

# Copy the handler source.
COPY handler.py ${LAMBDA_TASK_ROOT}/

# Lambda runtime invokes this function on each event.
CMD ["handler.lambda_handler"]

