import torch
from torch.utils.data import DataLoader, RandomSampler, BatchSampler
import numpy as np
import sklearn
from tqdm import tqdm_notebook as tqdm
import importance.utils as utils
from models.utils import SklearnClassifierWrapper


def estimate_total(model, dataset, batch_size, loss_fn):
    # Estimate expected sum of values.
    sequential_loader = DataLoader(dataset, batch_size=batch_size)
    N = 0
    mean_loss = 0
    marginal_pred = 0
    for x, y in sequential_loader:
        n = len(x)
        pred = model.predict(x.cpu().numpy())
        loss = loss_fn(pred, y.cpu().numpy())
        marginal_pred = (
            (N * marginal_pred + n * np.mean(pred, axis=0, keepdims=True))
            / (N + n))
        mean_loss = (N * mean_loss + n * np.mean(loss)) / (N + n)
        N += n

    # Mean loss of mean prediction.
    N = 0
    marginal_loss = 0
    for x, y in sequential_loader:
        n = len(x)
        marginal_pred_repeat = marginal_pred.repeat(len(y), 0)
        loss = loss_fn(marginal_pred_repeat, y.cpu().numpy())
        marginal_loss = (N * marginal_loss + n * np.mean(loss)) / (N + n)
        N += n
    return (marginal_loss - mean_loss)


def permutation_sampling(model,
                         dataset,
                         imputation_module,
                         loss,
                         batch_size,
                         n_samples,
                         m_samples,
                         detect_convergence=False,
                         convergence_threshold=0.01,
                         verbose=False,
                         bar=False):
    '''
    Estimates SAGE values by unrolling permutations of feature indices.

    Args:
      model: sklearn model.
      dataset: PyTorch dataset, such as data.utils.TabularDataset.
      imputation_module: for imputing held out values, such as
        utils.MarginalImputation.
      loss: string descriptor of loss function ('mse', 'cross entropy').
      batch_size: number of examples to be processed at once.
      n_samples: number of outer loop samples.
      m_samples: number of inner loop samples.
      detect_convergence: whether to detect convergence of SAGE estimates.
      convergence_threshold: confidence interval threshold for determining
        convergence. Represents portion of estimated sum of SAGE values.
      verbose: whether to print progress messages.
      bar: whether to display progress bar.
    '''
    # Add wrapper if necessary.
    if isinstance(model, sklearn.base.ClassifierMixin):
        model = SklearnClassifierWrapper(model)

    # Verify imputation module is valid.
    assert isinstance(imputation_module, utils.ImputationModule)
    if isinstance(imputation_module, utils.ReferenceImputation):
        if m_samples != 1:
            m_samples = 1
            print('Using ReferenceImputation, setting m_samples = 1')

    # Setup.
    input_size = dataset.input_size
    loader = DataLoader(
        dataset, batch_sampler=BatchSampler(
            RandomSampler(dataset, replacement=True,
                          num_samples=n_samples),
            batch_size=batch_size, drop_last=False),
        num_workers=4, pin_memory=True)
    loss_fn = utils.get_loss_np(loss, reduction='none')
    total = estimate_total(model, dataset, batch_size, loss_fn)

    # Print message explaining parameter choices.
    if verbose:
        print('{} samples per feature, minibatch size (batch x m) = {}'.format(
            n_samples, batch_size * m_samples))

    # For updating scores.
    tracker = utils.ImportanceTracker()

    if bar:
        bar = tqdm(total=n_samples * input_size)
    for x, y in loader:
        # Sample permutations.
        n = len(x)
        y = y.cpu().numpy()
        S = torch.zeros(
            n, input_size, dtype=torch.float32)
        permutations = torch.arange(input_size).repeat(n, 1)
        for i in range(n):
            permutations.data[i] = (
                permutations[i, torch.randperm(input_size)])
        S = S.repeat(m_samples, 1)
        permutations = permutations.repeat(m_samples, 1)

        # Make prediction with missing features.
        x = x.repeat(m_samples, 1)
        y_hat = model.predict(
            imputation_module.impute(x, S).cpu().numpy())
        y_hat = np.mean(
            y_hat.reshape((m_samples, -1, *y_hat.shape[1:])), axis=0)
        prev_loss = loss_fn(y_hat, y)

        # Setup.
        arange = np.arange(n)
        arange_long = np.arange(n * m_samples)
        scores = np.zeros((n, input_size))

        for i in range(input_size):
            # Add next feature.
            inds = permutations[:, i].numpy()
            S[arange_long, inds] = 1.0

            # Make prediction with missing features.
            y_hat = model.predict(
                imputation_module.impute(x, S).cpu().numpy())
            y_hat = np.mean(
                y_hat.reshape((m_samples, -1, *y_hat.shape[1:])), axis=0)
            loss = loss_fn(y_hat, y)

            # Calculate delta sample.
            scores[arange, inds[:n]] = prev_loss - loss
            prev_loss = loss
            if bar:
                bar.update(n)

        # Update tracker.
        tracker.update(scores)

        # Check for convergence.
        conf = np.max(tracker.var) ** 0.5
        if verbose:
            print('Conf = {:.4f}, Total = {:.4f}'.format(conf, total))
        if detect_convergence:
            if (conf / total) < convergence_threshold:
                if verbose:
                    print('Stopping early')
                break

    return tracker.scores


def iterated_sampling(model,
                      dataset,
                      imputation_module,
                      loss,
                      batch_size,
                      n_samples,
                      m_samples,
                      detect_convergence=False,
                      convergence_threshold=0.01,
                      verbose=False,
                      bar=False):
    '''
    Estimates SAGE values one at a time, by sampling subsets of features.

    Args:
      model: sklearn model.
      dataset: PyTorch dataset, such as data.utils.TabularDataset.
      imputation_module: for imputing held out values, such as
        utils.MarginalImputation.
      loss: string descriptor of loss function ('mse', 'cross entropy').
      batch_size: number of examples to be processed at once.
      n_samples: number of outer loop samples.
      m_samples: number of inner loop samples.
      detect_convergence: whether to detect convergence of SAGE estimates.
      convergence_threshold: confidence interval threshold for determining
        convergence. Represents portion of estimated sum of SAGE values.
      verbose: whether to print progress messages.
      bar: whether to display progress bar.
    '''
    # Add wrapper if necessary.
    if isinstance(model, sklearn.base.ClassifierMixin):
        model = SklearnClassifierWrapper(model)

    # Verify imputation module is valid.
    assert isinstance(imputation_module, utils.ImputationModule)
    if isinstance(imputation_module, utils.ReferenceImputation):
        if m_samples != 1:
            m_samples = 1
            print('Using ReferenceImputation, setting m_samples = 1')

    # Setup.
    input_size = dataset.input_size
    loader = DataLoader(
        dataset, batch_sampler=BatchSampler(
            RandomSampler(dataset, replacement=True,
                          num_samples=n_samples),
            batch_size=batch_size, drop_last=False),
        num_workers=4, pin_memory=True)
    loss_fn = utils.get_loss_np(loss, reduction='none')
    total = estimate_total(model, dataset, batch_size, loss_fn)

    # Print message explaining parameter choices.
    if verbose:
        print('{} permutations, minibatch size (batch x m) = {}'.format(
            n_samples, batch_size * m_samples))

    # For updating scores.
    scores = []

    if bar:
        bar = tqdm(total=n_samples * input_size)
    for ind in range(input_size):
        tracker = utils.ImportanceTracker()
        for x, y in loader:
            # Sample subset of features.
            y = y.cpu().numpy()
            n = len(x)
            S = utils.sample_subset_feature(input_size, n, ind)
            S = S.repeat(m_samples, 1)

            # Loss with feature excluded.
            x = x.repeat(m_samples, 1)
            y_hat = model.predict(
                imputation_module.impute(x, S).cpu().numpy())
            y_hat = np.mean(
                y_hat.reshape((m_samples, -1, *y_hat.shape[1:])), axis=0)
            loss_discluded = loss_fn(y_hat, y)

            # Loss with feature included.
            S[:, ind] = 1.0
            y_hat = model.predict(
                imputation_module.impute(x, S).cpu().numpy())
            y_hat = np.mean(
                y_hat.reshape((m_samples, -1, *y_hat.shape[1:])), axis=0)
            loss_included = loss_fn(y_hat, y)

            # Calculate delta sample.
            tracker.update(loss_discluded - loss_included)
            if bar:
                bar.update(n)

            # Check for convergence.
            conf = tracker.var ** 0.5
            if verbose:
                print('Imp = {:.4f}, Conf = {:.4f}, Total = {:.4f}'.format(
                    tracker.scores, conf, total))
            if detect_convergence:
                if (conf / total) < convergence_threshold:
                    if verbose:
                        print('Stopping feature early')
                    break

        # Save feature score.
        if verbose:
            print('Done with feature {}'.format(ind))
        scores.append(tracker.scores)

    return np.stack(scores)

