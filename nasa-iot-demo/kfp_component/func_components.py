"""Helper components."""

import os
from typing import NamedTuple
from kfp.v2.dsl import component
import kfp

PROJECT_ID = os.getenv('PROJECT_ID', 'mwpltr')
BASE_IMAGE = os.getenv('BASE_IMAGE', f"gcr.io/{PROJECT_ID}/nasa-iot-base:v1")


@component(output_component_file="load_raw_data.yaml", base_image=BASE_IMAGE)
def load_raw_data(source_bucket_name: str, 
                  prefix: str,
                  dest_bucket_name: str,
                  dest_file_name: str) -> NamedTuple('Outputs', [('dest_bucket_name', str), 
                                                                 ('dest_file_name', str)]):
    
    """Retrieves the sample files, combines them, and outputs the desting location in GCS."""
    import pandas as pd
    import numpy as np
    from io import StringIO
    from google.cloud import storage
    
    # Get the raw files out of GCS public bucket
    merged_data = pd.DataFrame()
    client = storage.Client()
    blobs = client.list_blobs(source_bucket_name, prefix=prefix)
    
    for blob in blobs:
        dataset = pd.read_csv("gs://{0}/{1}".format(source_bucket_name, blob.name), sep='\t')
        dataset_mean_abs = np.array(dataset.abs().mean())
        dataset_mean_abs = pd.DataFrame(dataset_mean_abs.reshape(1, 4))
        dataset_mean_abs.index = [blob.name.split("/")[-1]]
        merged_data = merged_data.append(dataset_mean_abs)
        
    merged_data.columns = ['bearing-1', 'bearing-2', 'bearing-3', 'bearing-4']
    
    # Transform data file index to datetime and sort in chronological order
    merged_data.index = pd.to_datetime(merged_data.index, format='%Y.%m.%d.%H.%M.%S')
    merged_data = merged_data.sort_index()
    
    # Drop the raw_data into a bucket
    f = StringIO()
    merged_data.to_csv(f)
    f.seek(0)
    client.get_bucket(dest_bucket_name).blob(dest_file_name).upload_from_file(f, content_type='text/csv')
    
    return (dest_bucket_name, dest_file_name)


@component(output_component_file="split_data.yaml", base_image=BASE_IMAGE)
def split_data(bucket_name: str, 
               source_file: str,
               split_time: str, 
               preprocess: bool) -> NamedTuple('Outputs', [('bucket_name', str), 
                                                           ('train_dest_file', str), 
                                                           ('test_dest_file', str)]):
    
    from sklearn.preprocessing import MinMaxScaler
    from google.cloud import storage
    import pandas as pd
    import numpy as np
    from io import StringIO
    import time
    
    # Read in the data from the GCS bucket and format the data
    data_loc = "gs://{0}/{1}".format(bucket_name, source_file)
    data = pd.read_csv(data_loc, index_col=0)
    #data.index.rename('time', inplace=True)
    first_idx = data.index.values[0]

    # Split the data based on the split_time param
    data = data.sort_index()
    train_data = data.loc[first_idx:split_time]  # Note: this is 'inclusive' so the last data point in train data
    test_data = data.loc[split_time:]            # shows up as the first data point in the test data
                                                 # This shouldn't be a big deal for this dataset
    
    # Preprocess the data (if applicable)
    if preprocess:
        scaler = MinMaxScaler()
        X_train = scaler.fit_transform(train_data)
        X_test = scaler.transform(test_data)
    
    else:
        X_train = train_data.to_numpy()
        X_test = test_data.to_numpy()
        
    scaled_train_data = pd.DataFrame(X_train, columns=data.columns)
    scaled_test_data = pd.DataFrame(X_test, columns=data.columns)
    
    # Save the data splits off to GCS bucket
    train_f = StringIO()
    test_f = StringIO()
    
    scaled_train_data.to_csv(train_f)
    scaled_test_data.to_csv(test_f)
    
    train_f.seek(0)
    test_f.seek(0)
    
    train_dest_file = "train_{}.csv".format(time.perf_counter())
    test_dest_file = "test_{}.csv".format(time.perf_counter())
    
    client = storage.Client()
    client.get_bucket(bucket_name).blob(train_dest_file).upload_from_file(train_f, content_type='text/csv')
    client.get_bucket(bucket_name).blob(test_dest_file).upload_from_file(test_f, content_type='text/csv')
    
    # Return the location of the new data splits
    return (bucket_name, train_dest_file, test_dest_file)


@component(output_component_file="vertex_custom_job.yaml", base_image=BASE_IMAGE)
def vertex_custom_job(
    project: str,
    display_name: str,
    container_image_uri: str,
    train_args: str,
    location: str = "us-central1",
    api_endpoint: str = "us-central1-aiplatform.googleapis.com",
    machine_type: str = "n1-standard-4",
    accelerator_type: str = None,
    accelerator_count: int = None) -> NamedTuple('Outputs', [('job_id', str),
                                                             ('display_name', str)]):

    from google.cloud import aiplatform
    import json

    # The AI Platform services require regional API endpoints.
    client_options = {"api_endpoint": api_endpoint}
    # Initialize client that will be used to create and send requests.
    # This client only needs to be created once, and can be reused for multiple requests.
    client = aiplatform.gapic.JobServiceClient(client_options=client_options)

    train_args = json.loads(train_args)

    custom_job = {
        "display_name": display_name,
        "job_spec": {
            "worker_pool_specs": [
                {
                    "machine_spec": {
                        "machine_type": machine_type,
                        "accelerator_type": accelerator_type,
                        "accelerator_count": accelerator_count,
                    },
                    "replica_count": 1,
                    "container_spec": {
                        "image_uri": container_image_uri,
                        "command": [],
                        "args": train_args,
                    },
                }
            ]
        },
    }
    parent = f"projects/{project}/locations/{location}"
    response = client.create_custom_job(parent=parent, custom_job=custom_job)
    print("response:", response)

    job_id = str(response.name).split("/")[-1]
    display_name = str(response.display_name)

    return (job_id, display_name)


# For testing
if __name__ == "__main__":
    import json

    train_args = json.dumps([
        "--bucket", "rrusson-bucket",
        "--train_file", "train_481.013178969.csv",
        "--test_file", "test_481.013187969.csv",
        "--job_dir", "gs://rrusson-bucket",
    ])

    job_id, display_name = vertex_custom_job(
        "mwpmltr",
        "nasa-iot-submission-test",
        "gcr.io/mwpmltr/nasa-iot-trainer:v5",
        train_args, 
    )

    print(job_id)
    print(display_name)