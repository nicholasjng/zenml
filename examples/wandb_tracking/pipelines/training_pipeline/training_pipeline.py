#  Copyright (c) ZenML GmbH 2020. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at:
#
#       https://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express
#  or implied. See the License for the specific language governing
#  permissions and limitations under the License.
from zenml.integrations.constants import WANDB, TENSORFLOW
from zenml.pipelines import pipeline


@pipeline(enable_cache=False, required_integrations=[WANDB, TENSORFLOW])
def wandb_example_pipeline(
    importer,
    normalizer,
    trainer,
    evaluator,
):
    # Link all the steps artifacts together
    x_train, y_train, x_test, y_test = importer()
    x_trained_normed, x_test_normed = normalizer(x_train=x_train, x_test=x_test)
    model = trainer(
        x_train=x_trained_normed, y_train=y_train, x_val=x_test, y_val=y_test
    )
    evaluator(x_test=x_test_normed, y_test=y_test, model=model)
